"""A model is stored only where it reads back the same, judged at each position's declared type.

Under `Any`, a dict, a list and a `set[Any]` compare by JSON form, NaN equal to NaN, item by item: a
NaN written as `null` is not the NaN it was. A `Sequence` holding a tuple reads back as a list, which
is not the tuple. A fixed tuple compares each item at its own type, and every tuple compares its
items, not only its length. A comparison that raises, or a JSON form that cannot be made, is not the
same. A model or a dataclass read back as its base class is not the same. A union arm holds a value
only by strict validation, so a list arm does not hold a tuple.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from typing import Annotated, Any

import pytest
from cliffracer_kv.errors import ModelDoesNotReadBackError
from cliffracer_kv.serialization import serialize_value
from pydantic import BaseModel, ConfigDict, PlainSerializer, PlainValidator

pytestmark = pytest.mark.unit


class Constants(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")
    v: Any


def test_a_dict_under_any_holding_nan_reads_back_the_same():
    assert serialize_value(Constants(v={"a": math.nan})) == b'{"v":{"a":NaN}}'


def test_a_list_under_any_holding_nan_reads_back_the_same():
    assert serialize_value(Constants(v=[math.nan])) == b'{"v":[NaN]}'


class Loose(BaseModel):
    v: Any


def test_a_list_under_any_whose_nan_is_written_null_is_refused():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(Loose(v=[math.nan]))


class OptionalFloat(BaseModel):
    f: float | None


def test_an_optional_float_whose_nan_is_written_null_is_refused():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(OptionalFloat(f=math.nan))


class SetOfAny(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")
    s: set[Any]


def test_a_set_of_any_compares_by_json_form():
    """No `==` of two sets holding distinct NaN objects is true."""
    assert serialize_value(SetOfAny(s={math.nan})) == b'{"s":[NaN]}'


class SeqOfInt(BaseModel):
    s: Sequence[int]


def test_a_sequence_holding_a_tuple_reads_back_as_a_list_and_is_refused():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(SeqOfInt(s=(1, 2)))


class FixedTuple(BaseModel):
    t: tuple[int, Any]


def test_a_fixed_tuple_compares_each_item_at_its_own_type():
    """The `Any` item reads back as a list and compares by JSON form."""
    assert serialize_value(FixedTuple(t=(1, (2, 3)))) == b'{"t":[1,[2,3]]}'


class TupleOfOptional(BaseModel):
    t: tuple[float | None, ...]


def test_a_tuple_compares_its_items_not_only_its_length():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(TupleOfOptional(t=(math.nan,)))


class Unequal:
    def __eq__(self, other: object) -> bool:
        raise RuntimeError("no comparison")

    __hash__ = object.__hash__


def _unequal(value: Any) -> Unequal:
    return value if isinstance(value, Unequal) else Unequal()


class HoldsUnequal(BaseModel):
    w: Annotated[Unequal, PlainValidator(_unequal), PlainSerializer(lambda v: "w")]


def test_a_comparison_that_raises_is_not_the_same():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(HoldsUnequal(w=Unequal()))


class Opaque:
    pass


class HoldsOpaque(BaseModel):
    w: Annotated[Any, PlainSerializer(lambda v: "w")]


def test_a_json_form_that_cannot_be_made_is_not_the_same():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(HoldsOpaque(w=Opaque()))


@dataclasses.dataclass
class DcBase:
    x: int


@dataclasses.dataclass
class DcLeaf(DcBase):
    pass


class HoldsDc(BaseModel):
    d: DcBase


def test_a_dataclass_read_back_as_its_base_is_refused():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(HoldsDc(d=DcLeaf(1)))


class ChildBase(BaseModel):
    x: int


class ChildLeaf(ChildBase):
    pass


class HoldsChild(BaseModel):
    c: ChildBase


def test_a_model_read_back_as_its_base_is_refused():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(HoldsChild(c=ChildLeaf(x=1)))


class LaxArm(BaseModel):
    v: list[int] | Any


def test_a_union_arm_holds_a_value_only_by_strict_validation():
    """The list arm holds a tuple only laxly, so the tuple is judged under `Any`, by JSON form."""
    assert serialize_value(LaxArm(v=(1, 2))) == b'{"v":[1,2]}'

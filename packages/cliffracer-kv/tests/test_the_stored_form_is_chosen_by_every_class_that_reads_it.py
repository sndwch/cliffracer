"""Which form a model with base classes is stored in, and what a refusal names.

A form every class reads back as the model is stored; with a NaN no read-back is `==` the model, so
that judgement decides. A base is judged on the fields it does not exclude, against its own reading
of the model's values by field name, and a base reading a tuple of another length reads other
values. The third form writes each level by field name first, as utf-8.

When no form serves every class, the released form is the aliases when the model reads only those,
and the field names when it reads neither. A form the model reads is stored only where each base
reads it back, or fails to read it where it failed to read the released form too. A refusal names
each form the model was written in and why it was refused, and names a base only for a form the
model reads.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from cliffracer_kv.errors import ModelDoesNotReadBackError
from cliffracer_kv.serialization import serialize_value
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field

pytestmark = pytest.mark.unit

_NAN = ConfigDict(ser_json_inf_nan="constants")
_NAN_BY_NAME = ConfigDict(populate_by_name=True, ser_json_inf_nan="constants")

# Each leaf in this section holds a NaN, so whether every class reads a form back as the model
# decides the choice, and its computed field leaves no third form. Its base reads only the aliases.


class NanBase(BaseModel):
    model_config = _NAN
    f: float = Field(alias="F")


class NanLeaf(NanBase):
    model_config = _NAN_BY_NAME

    @computed_field  # type: ignore[prop-decorator]
    @property
    def c(self) -> int:
        return 1


def test_a_form_every_class_reads_is_stored_though_nan_is_not_equal_to_itself():
    assert serialize_value(NanLeaf(F=math.nan)) == b'{"F":NaN,"c":1}'


class ExcludeBase(BaseModel):
    model_config = _NAN
    f: float = Field(alias="F")
    note: str = Field("d", exclude=True)


class ExcludeLeaf(ExcludeBase):
    model_config = _NAN_BY_NAME

    @computed_field  # type: ignore[prop-decorator]
    @property
    def c(self) -> int:
        return 1


def test_a_base_does_not_compare_a_field_it_excludes():
    assert serialize_value(ExcludeLeaf(F=math.nan, note="x")) == b'{"F":NaN,"c":1}'


class TupleBase(BaseModel):
    model_config = _NAN
    f: float = Field(alias="F")
    t: tuple[int, ...]


class TupleLeaf(TupleBase):
    model_config = _NAN_BY_NAME
    t: list[int]  # type: ignore[assignment]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def c(self) -> int:
        return 1


def test_a_base_is_judged_against_its_own_reading_of_the_values():
    """TupleBase makes a tuple of the leaf's list, and reads a tuple back."""
    assert serialize_value(TupleLeaf(F=math.nan, t=[1, 2])) == b'{"F":NaN,"t":[1,2],"c":1}'


# --- the third form ------------------------------------------------------------------------------


class InnerByName(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    i: int = Field(alias="I")


class OuterThird(BaseModel):
    o: int = Field(validation_alias="oo")
    inner: InnerByName
    word: str = "é"


def test_the_third_form_writes_each_level_by_field_name_first_and_as_utf8():
    stored = serialize_value(OuterThird(oo=1, inner=InnerByName(i=2)))

    assert stored == '{"inner":{"i":2},"word":"é","oo":1}'.encode()


class LengthBase(BaseModel):
    t: tuple[int, ...] = Field(validation_alias=AliasChoices("u"))


class LengthLeaf(LengthBase):
    y: int = Field(validation_alias="yy", serialization_alias="Y")
    t: tuple[int, ...]
    u: tuple[int, ...]


def test_a_base_reading_a_tuple_of_another_length_reads_other_values():
    """The model reads only the third form, and LengthBase reads `t` from `u`, which begins as `t`
    does and is shorter."""
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(LengthLeaf(yy=1, t=(1, 2), u=(1,)))


# --- no form serves every class ------------------------------------------------------------------


class AliasBase(BaseModel):
    y: int


class AliasLeaf(AliasBase):
    y: int = Field(alias="Y")


def test_the_released_form_is_the_aliases_when_the_model_reads_only_those():
    assert serialize_value(AliasLeaf(Y=3)) == b'{"Y":3}'


class ReleasedBase(BaseModel):
    y: int = Field(validation_alias=AliasChoices("y", "Y"))


class ReleasedLeaf(ReleasedBase):
    y: int = Field(validation_alias="yy", serialization_alias="Y")


def test_a_base_that_read_the_released_form_is_not_made_to_fail():
    """The model reads neither dump, so the released form is the field names. ReleasedBase reads
    those and not the third form, the only one the model reads, and is named for it."""
    with pytest.raises(ModelDoesNotReadBackError) as refused:
        serialize_value(ReleasedLeaf(yy=2))

    assert str(refused.value).count(", while ReleasedBase") == 1


class TwoBase(BaseModel):
    a: int


class TwoMid(TwoBase):
    z: int = Field(validation_alias="zz")


class TwoLeaf(TwoMid):
    z: int = Field(validation_alias="yy", serialization_alias="Y")


def test_a_base_that_reads_a_form_back_passes_whatever_it_made_of_the_released_one():
    """TwoBase reads the third form and the released one; TwoMid reads neither."""
    assert serialize_value(TwoLeaf(a=1, yy=2)) == b'{"a":1,"yy":2}'


# --- what a refusal names ------------------------------------------------------------------------


class PlainBase(BaseModel):
    y: int


class PlainLeaf(PlainBase):
    y: int = Field(validation_alias="yy", serialization_alias="Y")


def test_a_form_the_model_cannot_read_names_no_base():
    """PlainBase cannot read the aliases, though it reads the released field names. The model reads
    neither."""
    with pytest.raises(ModelDoesNotReadBackError) as refused:
        serialize_value(PlainLeaf(yy=2))

    assert ", while" not in str(refused.value)


class Broken(BaseModel):
    x: int


@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")
def test_a_refusal_names_each_form_written_and_why_it_was_refused():
    with pytest.raises(ModelDoesNotReadBackError) as refused:
        serialize_value(Broken.model_construct(x="abc"))

    text = str(refused.value)
    assert "validation aliases" not in text
    assert text.count("is refused (1 error(s), first: ") == 2


class Raises(BaseModel):
    x: int

    @classmethod
    def model_validate_json(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise RuntimeError("no read")


def test_a_refusal_names_an_error_other_than_a_validation_error_by_its_type():
    with pytest.raises(ModelDoesNotReadBackError, match=r"is refused \(RuntimeError\)"):
        serialize_value(Raises(x=1))


# A leaf that reads field names over a base that reads its alias alone, with no NaN: the leaf's
# own class reads the field-name form back as equal, and only the alias form serves both.


class AliasOnlyBase(BaseModel):
    f: float = Field(alias="F")


class ByNameLeaf(AliasOnlyBase):
    model_config = ConfigDict(populate_by_name=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def c(self) -> int:
        return 1


def test_a_form_only_the_models_own_class_reads_is_not_chosen_before_one_every_class_reads():
    stored = serialize_value(ByNameLeaf(F=1.0))

    assert stored == b'{"F":1.0,"c":1}'
    assert AliasOnlyBase.model_validate_json(stored).f == 1.0

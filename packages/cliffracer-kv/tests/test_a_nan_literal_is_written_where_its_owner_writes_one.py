"""A NaN or an infinity is stored as a JSON literal exactly where the model's own dump writes one.

When neither dump of a model reads back, it is written where its validation aliases read it, and a
NaN or an infinity in it has no JSON number. The literal is written when Pydantic's own
`model_dump_json` writes every NaN and infinity of that form as a constant, and a value holding one
the dump writes as null is refused. Pydantic decides where: usually the config of the model holding
the number (`ser_json_inf_nan="constants"` writes the literal, the default null), whatever the
config of a model or dataclass beside it that holds none; but a model in an `OrderedDict`, a
`Sequence` or a union of models, and an extra, are written under the outermost model's config.
"""

from __future__ import annotations

import collections
import dataclasses
from collections.abc import Sequence
from typing import Annotated, Optional, Union

import pydantic.dataclasses
import pytest
from cliffracer_kv import ModelDoesNotReadBackError
from cliffracer_kv.serialization import (
    _written_as_the_models_dump_writes_it,
    deserialize_value,
    serialize_value,
)
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PlainSerializer

pytestmark = pytest.mark.unit

CONSTANTS = ConfigDict(ser_json_inf_nan="constants")
NAN = float("nan")


class WritesNull(BaseModel):
    f: float = 0.0


@pydantic.dataclasses.dataclass(config=CONSTANTS)
class DataclassWritesConstants:
    g: float = 0.0


class OwnsNanBesideANullWriter(BaseModel):
    model_config = CONSTANTS

    f: float = Field(0.0, validation_alias=AliasChoices("ff"))
    i: WritesNull = Field(default_factory=WritesNull)


class OwnsNanBesideNullWriters(BaseModel):
    model_config = CONSTANTS

    f: float = Field(0.0, validation_alias=AliasChoices("ff"))
    xs: list[WritesNull] = Field(default_factory=list)


class OwnsNanBesideAMember(BaseModel):
    model_config = CONSTANTS

    f: float = Field(0.0, validation_alias=AliasChoices("ff"))
    u: Optional[WritesNull] = None  # noqa: UP045 -- the member of a union is what is walked


@dataclasses.dataclass
class StdlibPoint:
    g: float = 0.0


class ConstantsWriterHoldsAStdlibDataclass(BaseModel):
    model_config = CONSTANTS

    f: float = Field(0.0, validation_alias=AliasChoices("ff"))
    d: StdlibPoint = Field(default_factory=StdlibPoint)


class NullWriterHoldsADataclassThatWritesConstants(BaseModel):
    k: int = Field(0, validation_alias=AliasChoices("kk"))
    d: DataclassWritesConstants = Field(default_factory=DataclassWritesConstants)
    i: WritesNull = Field(default_factory=WritesNull)


class NullWriterWithExtras(BaseModel):
    model_config = ConfigDict(extra="allow")

    f: float = 0.0


class OwnsNanBesideExtras(BaseModel):
    model_config = CONSTANTS

    f: float = Field(0.0, validation_alias=AliasChoices("ff"))
    e: NullWriterWithExtras = Field(default_factory=NullWriterWithExtras)


class ConstantsWriterWithExtras(BaseModel):
    model_config = ConfigDict(extra="allow", ser_json_inf_nan="constants")

    f: float = 0.0


class NullWriterHoldingExtras(BaseModel):
    k: int = Field(0, validation_alias=AliasChoices("kk"))
    e: ConstantsWriterWithExtras = Field(default_factory=ConstantsWriterWithExtras)


class HoldsNullWritersByKey(BaseModel):
    model_config = CONSTANTS

    k: int = Field(0, validation_alias=AliasChoices("kk"))
    by_name: dict[str, WritesNull] = Field(default_factory=dict)


@pytest.mark.parametrize(
    ("value", "dumped", "stored"),
    [
        (
            OwnsNanBesideANullWriter.model_validate({"ff": NAN, "i": {"f": 2.0}}),
            '{"f":NaN,"i":{"f":2.0}}',
            b'{"i":{"f":2.0},"ff":NaN}',
        ),
        (
            OwnsNanBesideNullWriters.model_validate({"ff": NAN, "xs": [{"f": 1.0}, {"f": 2.0}]}),
            '{"f":NaN,"xs":[{"f":1.0},{"f":2.0}]}',
            b'{"xs":[{"f":1.0},{"f":2.0}],"ff":NaN}',
        ),
        (
            OwnsNanBesideAMember.model_validate({"ff": NAN, "u": {"f": 3.0}}),
            '{"f":NaN,"u":{"f":3.0}}',
            b'{"u":{"f":3.0},"ff":NaN}',
        ),
        (
            NullWriterHoldsADataclassThatWritesConstants.model_validate(
                {"kk": 1, "d": {"g": NAN}, "i": {"f": 1.0}}
            ),
            '{"k":1,"d":{"g":NaN},"i":{"f":1.0}}',
            b'{"d":{"g":NaN},"i":{"f":1.0},"kk":1}',
        ),
        (
            OwnsNanBesideANullWriter.model_validate({"ff": float("-inf"), "i": {"f": 2.0}}),
            '{"f":-Infinity,"i":{"f":2.0}}',
            b'{"i":{"f":2.0},"ff":-Infinity}',
        ),
        (
            ConstantsWriterHoldsAStdlibDataclass.model_validate({"ff": 1.0, "d": {"g": NAN}}),
            '{"f":1.0,"d":{"g":NaN}}',
            b'{"d":{"g":NaN},"ff":1.0}',
        ),
    ],
    ids=[
        "beside-a-model-that-writes-null",
        "beside-a-list-of-them",
        "beside-a-union-member",
        "owned-by-a-dataclass-under-a-model-that-writes-null",
        "an-infinity-beside-a-model-that-writes-null",
        "held-by-a-stdlib-dataclass-of-a-model-that-writes-constants",
    ],
)
def test_a_nan_owned_by_a_model_that_writes_constants_is_stored_as_the_literal_its_dump_writes(
    value, dumped, stored
):
    assert value.model_dump_json() == dumped

    data = serialize_value(value)

    assert data == stored
    read = deserialize_value(data, as_type=type(value))
    assert sorted(map(repr, _floats(read))) == sorted(map(repr, _floats(value)))


@pytest.mark.parametrize(
    "value",
    [
        OwnsNanBesideAMember.model_validate({"ff": 1.0, "u": {"f": NAN}}),
        HoldsNullWritersByKey.model_validate({"kk": 1, "by_name": {"a": {"f": NAN}}}),
    ],
    ids=[
        "a-union-member-that-writes-null",
        "a-dict-value-that-writes-null",
    ],
)
def test_a_nan_owned_by_a_model_that_writes_null_is_refused(value):
    assert "null" in value.model_dump_json()

    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(value)


def test_CONTROL_the_same_dict_holding_a_finite_number_is_stored():
    value = HoldsNullWritersByKey.model_validate({"kk": 1, "by_name": {"a": {"f": 1.0}}})

    assert serialize_value(value) == b'{"by_name":{"a":{"f":1.0}},"kk":1}'


def _floats(value):
    if isinstance(value, float):
        yield value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _floats(getattr(value, name))
    elif hasattr(value, "__dataclass_fields__"):
        for name in value.__dataclass_fields__:
            yield from _floats(getattr(value, name))
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _floats(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _floats(item)


class WritesConstants(BaseModel):
    model_config = CONSTANTS

    f: float = 0.0


class Other(BaseModel):
    g: int = 0


def _holding(annotation, value):
    """A default-config model with one field of `annotation`, set to `value`."""
    model = type(
        "Holder",
        (BaseModel,),
        {"__annotations__": {"x": annotation}, "x": None},
    )
    return model(x=value)


@pytest.mark.parametrize(
    ("value", "dumped", "written"),
    [
        (
            _holding(
                collections.OrderedDict[str, WritesConstants],
                collections.OrderedDict(a=WritesConstants(f=NAN)),
            ),
            '{"x":{"a":{"f":null}}}',
            {"x": {"a": {"f": NAN}}},
        ),
        (
            _holding(Sequence[WritesConstants], [WritesConstants(f=NAN)]),
            '{"x":[{"f":null}]}',
            {"x": [{"f": NAN}]},
        ),
        (
            _holding(Union[WritesConstants, Other], WritesConstants(f=NAN)),  # noqa: UP007
            '{"x":{"f":null}}',
            {"x": {"f": NAN}},
        ),
        (
            OwnsNanBesideExtras.model_validate({"ff": 1.0, "e": {"f": 1.0, "extra": NAN}}),
            '{"f":1.0,"e":{"f":1.0,"extra":NaN}}',
            None,
        ),
        (
            NullWriterHoldingExtras.model_validate({"kk": 1, "e": {"f": 1.0, "extra": NAN}}),
            '{"k":1,"e":{"f":1.0,"extra":null}}',
            {"k": 1, "e": {"f": 1.0, "extra": NAN}},
        ),
    ],
    ids=[
        "a-constants-model-in-an-ordered-dict",
        "a-constants-model-in-a-sequence",
        "a-constants-model-in-a-union-of-models",
        "an-extra-under-an-outermost-model-that-writes-constants",
        "an-extra-under-an-outermost-model-that-writes-null",
    ],
)
def test_the_literal_follows_where_pydantics_own_dump_writes_one(value, dumped, written):
    """A model in an OrderedDict, a Sequence or a union of models, and an extra, are written under
    the outermost model's config, not their own: the rule asks the dump rather than the config of
    the model holding the number. `written` is the form with the NaN kept; None means the dump's own
    form (the extra's constants case, where the dump writes the literal)."""
    assert value.model_dump_json() == dumped
    literal = written is None
    if written is None:
        written = {"f": 1.0, "e": {"f": 1.0, "extra": NAN}}

    assert _written_as_the_models_dump_writes_it(value, written) is literal


class MadeNan(BaseModel):
    """A field whose serializer writes a NaN from a finite value, beside one that holds a NaN."""

    made: Annotated[float, PlainSerializer(lambda v: float("nan"), return_type=float)] = 0.0
    held: float = 0.0


def test_a_serializer_that_writes_a_nan_does_not_stand_for_a_nan_written_as_null():
    """The dump writes one constant (the serializer's, here as null too) or none, while the form
    holds two NaNs: the counts differ and the literal is withheld."""
    value = MadeNan(made=1.0, held=NAN)
    written = {"made": NAN, "held": NAN}

    assert _written_as_the_models_dump_writes_it(value, written) is False


class MadeNanUnderConstants(BaseModel):
    model_config = CONSTANTS

    made: Annotated[float, PlainSerializer(lambda v: float("nan"), return_type=float)] = 0.0
    inner: Optional[WritesNull] = None  # noqa: UP045


def test_a_serializer_made_nan_beside_a_nan_written_as_null_is_refused():
    """The case a count of constants alone would pass: the dump writes the serializer's NaN as a
    constant and the inner model's NaN as null, one constant for one real NaN. The form holds both
    NaNs, two to the dump's one, so the literal is withheld."""
    value = MadeNanUnderConstants(made=1.0, inner=WritesNull(f=NAN))
    assert value.model_dump_json() == '{"made":NaN,"inner":{"f":null}}'
    written = {"made": NAN, "inner": {"f": NAN}}

    assert _written_as_the_models_dump_writes_it(value, written) is False

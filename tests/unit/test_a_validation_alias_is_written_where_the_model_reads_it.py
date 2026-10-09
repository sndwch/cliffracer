"""A field read through a `validation_alias` is written where the model reads it.

A dump writes a field's name, or its serialization alias, never its validation alias. A model whose
field is read only through `AliasChoices` or `AliasPath` therefore read every whole-value form as the
field's default: `x=5` arrived as `x=0`, with no error, and a required field was refused. Each level
now has one more candidate, the dump with each such field moved to the first `AliasChoices` member or
into the structure its `AliasPath` names. A generated client keeps it only where the declared
annotation reads it back as the argument; `call_rpc` and the proxy, which have no annotation, only
where no model class of the argument's hierarchy reads it as other values or reads an earlier form
instead.
"""

from datetime import UTC, datetime

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import read_python_then_json, wire_models

pytestmark = pytest.mark.unit


class Choices(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("a", "b"))


class ChoicesPathFirst(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices(AliasPath("outer", 0), "b"))


class Path(BaseModel):
    x: int = Field(0, validation_alias=AliasPath("outer", 0))


class DeepPath(BaseModel):
    x: int = Field(0, validation_alias=AliasPath("a", "b", 1))


class RequiredPath(BaseModel):
    x: int = Field(validation_alias=AliasPath("outer", "x"))


class Mixed(BaseModel):
    """An ordinary alias beside a validation alias, read by alias only."""

    y: int = Field(alias="yy")
    x: int = Field(0, validation_alias=AliasChoices("x2", "x3"))


class SerializedBeside(BaseModel):
    """A field written under a serialization alias but read by its name, beside a validation alias:
    neither whole-value dump reads back, and the form keeps the plain field under its name."""

    y: int = Field(serialization_alias="y_out")
    x: int = Field(0, validation_alias=AliasChoices("a", "b"))


class SharedPrefix(BaseModel):
    first: int = Field(0, validation_alias=AliasPath("pair", 0))
    second: int = Field(0, validation_alias=AliasPath("pair", 1))


class Inner(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("a", "b"))


class Outer(BaseModel):
    """The inner level needs the new form; the outer is read by field name only."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    n: int = Field(alias="N")
    inner: Inner


class Lowered(BaseModel):
    """A validator that normalises: no form reads back equal, and the call goes out as it did."""

    name: str = Field("", validation_alias=AliasChoices("n", "nm"))

    @field_validator("name")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.lower() + "!"


class Base9(BaseModel):
    x: int = Field(0, validation_alias=AliasChoices("y", "x"))


class Sub9(Base9):
    """Reads `x` under `z`; its base reads it under `x`, which is the dump's own key."""

    x: int = Field(0, validation_alias="z")


class TwoFieldsOneKey(BaseModel):
    """`a` and `b` are both read from `b`, and `p` first from `r`: every dump is refused (a dict
    `p` is read from `b` too), and the validation-alias form is accepted but read with `b`
    holding `a`'s value."""

    a: int = Field(validation_alias="b")
    b: int = Field(validation_alias="b")
    p: dict = Field(validation_alias=AliasChoices("r", "b"))


class AppModel(BaseModel):
    """What a project puts at the top of its models: configuration and no fields."""

    model_config = ConfigDict(str_strip_whitespace=True)


class ChoicesOverAppModel(AppModel):
    x: int = Field(0, validation_alias=AliasChoices("xx"))


class WithDefaults(BaseModel):
    note: str = "n"


class ChoicesOverDefaults(WithDefaults):
    x: int = Field(0, validation_alias=AliasChoices("xx"))


class Sub9WritesAnAlias(Base9):
    """Written by alias (`X`), read under `z`; its base reads `x`. `_encode` never offers the dump by
    field name, so that dump must not keep the validation-alias form from this path."""

    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(0, validation_alias="z", serialization_alias="X")


class ABase(BaseModel):
    x: int = 0


class ASub(ABase):
    """Reads `x` under `xx`; its base declares `x` with no alias and reads it by name."""

    x: int = Field(0, validation_alias=AliasChoices("xx"))


class PS(BaseModel):
    """A plain alias beside a validation alias, on one model."""

    x: int = Field(0, alias="X")
    y: int = Field(0, validation_alias=AliasChoices("yy"))


class MBBase(BaseModel):
    """Reads `p` under `q` or `b`, and `a` and `b` by name."""

    a: int = 0
    p: dict = Field({}, validation_alias=AliasChoices("q", "b"))
    b: int = 0


class MBSub(MBBase):
    """Reads `a` at `q.k`, `p` by name and `b` under `a`: MBBase accepts the form this class reads
    back, and reads it as other values."""

    a: int = Field(0, validation_alias=AliasPath("q", "k"))
    p: dict = Field({})
    b: int = Field(0, alias="a")


class ZBase(BaseModel):
    y: int = 0
    z: int = Field(validation_alias=AliasPath("zz", "k"))


class ZSub(ZBase):
    """`y` is read under `Y`; ZBase reads `{"zz": {"k": 9}}` but takes `y` by name, so it reads
    this class's validation-alias form with `y` at its default."""

    model_config = ConfigDict(extra="forbid")
    y: int = Field(alias="Y")


class FBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: int = 0


class FSub(FBase):
    """Reads `x` only under `xx`; FBase refuses `xx` as an extra key and reads `x` by name."""

    x: int = Field(0, validation_alias=AliasChoices("xx"))


class HoldsASub(BaseModel):
    item: ASub


class StrictZBase(BaseModel):
    model_config = ConfigDict(strict=True)
    when: datetime
    y: int = 0
    z: int = Field(validation_alias=AliasPath("zz", "k"))


class StrictZSub(StrictZBase):
    """StrictZBase refuses every dump (`z` is required at `zz.k`), and reads this class's
    validation-alias form in JSON mode only (a strict `datetime` from a string), with `y=0`."""

    model_config = ConfigDict(extra="forbid")
    y: int = Field(alias="Y")


def _encode(value, annotation):
    return ServiceClient._encode(object.__new__(ServiceClient), value, annotation)


CASES = [
    pytest.param(Choices(a=5), Choices, {"a": 5}, id="alias-choices"),
    pytest.param(
        ChoicesPathFirst(outer=[5]), ChoicesPathFirst, {"outer": [5]}, id="choices-path-first"
    ),
    pytest.param(Path(outer=[5]), Path, {"outer": [5]}, id="alias-path"),
    pytest.param(
        DeepPath(a={"b": [0, 5]}), DeepPath, {"a": {"b": [None, 5]}}, id="deep-alias-path"
    ),
    pytest.param(
        RequiredPath(outer={"x": 5}), RequiredPath, {"outer": {"x": 5}}, id="required-path"
    ),
    pytest.param(Mixed(yy=1, x2=5), Mixed, {"yy": 1, "x2": 5}, id="mixed-with-an-alias"),
    pytest.param(
        SerializedBeside(y=1, a=5),
        SerializedBeside,
        {"y": 1, "a": 5},
        id="serialization-alias-beside",
    ),
    pytest.param(
        SharedPrefix(pair=[1, 2]), SharedPrefix, {"pair": [1, 2]}, id="two-paths-one-list"
    ),
    pytest.param(
        Outer(n=1, inner=Inner(a=5)), Outer, {"n": 1, "inner": {"a": 5}}, id="nested-level"
    ),
]


@pytest.mark.parametrize(("value", "annotation", "wire"), CASES)
def test_an_rpc_call_writes_the_validation_alias_where_the_model_reads_it(value, annotation, wire):
    sent = wire_models({"item": value})["item"]

    assert sent == wire
    assert TypeAdapter(annotation).validate_python(sent) == value


@pytest.mark.parametrize(("value", "annotation", "wire"), CASES)
def test_a_generated_client_writes_it_the_same_way(value, annotation, wire):
    sent = _encode(value, annotation)

    assert sent == wire
    assert TypeAdapter(annotation).validate_python(sent) == value


def test_a_normalising_validator_gets_the_form_that_carries_its_value():
    """Nothing reads back equal: the validator changes the value each time it reads it. The form
    sent is the one whose reading is what the validator makes of the caller's own value (`"ab!"`
    read again is `"ab!!"`), not the dump by field name, which this model reads as its default."""
    value = Lowered(n="Ab")

    assert wire_models({"item": value})["item"] == {"n": "ab!"}
    assert _encode(value, Lowered) == {"n": "ab!"}
    assert Lowered.model_validate({"n": "ab!"}).name == "ab!!"
    assert Lowered.model_validate({"name": "ab!"}).name == ""


def test_the_form_never_displaces_a_dump_a_class_of_the_hierarchy_reads():
    """The handler may declare the base, which reads the dump `{"x": 5}` as the argument. The
    subclass reads `{"z": 5}`, but that form must not replace the dump a declaring class reads:
    sent, the base would read it as `x=0`."""
    value = Sub9.model_validate({"z": 5})

    assert wire_models({"item": value})["item"] == {"x": 5}
    assert _encode(value, Base9) == {"x": 5}
    assert Base9.model_validate({"x": 5}).x == 5


def test_a_form_the_model_reads_as_other_values_is_not_sent_in_place_of_a_refusal():
    """Every dump is refused, and the receiver says so. The validation-alias form is accepted but
    read with `b` holding `a`'s value, so it is not offered: the call goes out as before and is
    refused, rather than delivered wrong without a word."""
    value = TwoFieldsOneKey.model_validate(
        {"a": 89, "b": 55, "p": {"k": 4}}, by_name=True, by_alias=False
    )
    as_before = {"a": 89, "b": 55, "p": {"k": 4}}

    assert wire_models({"item": value})["item"] == as_before
    assert _encode(value, TwoFieldsOneKey) == as_before


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        pytest.param(
            ChoicesOverAppModel.model_validate({"xx": 5}), {"xx": 5}, id="config-only-base"
        ),
        pytest.param(
            ChoicesOverDefaults.model_validate({"xx": 5}),
            {"note": "n", "xx": 5},
            id="defaults-only-base",
        ),
    ],
)
def test_a_base_that_declares_no_validation_alias_has_no_say_in_the_form(value, wire):
    """A base with no fields, or only defaulted ones, reads any dump as "equal" in the fields it
    declares; it must not keep the validation-alias form from a subclass that needs it."""
    assert wire_models({"item": value})["item"] == wire
    assert _encode(value, type(value)) == wire


def test_only_the_forms_a_path_offers_keep_the_validation_alias_form_away():
    value = Sub9WritesAnAlias.model_validate({"z": 5})

    assert _encode(value, Sub9WritesAnAlias) == {"z": 5}


def test_a_base_declaring_the_field_by_name_keeps_the_dump_it_reads():
    """A handler declaring the base reads `{"x": 5}` as 5 and `{"xx": 5}` as its default: the
    base reads a form offered earlier and not the validation-alias form, so that form is not sent."""
    value = ASub.model_validate({"xx": 5})

    assert wire_models({"item": value})["item"] == {"x": 5}
    assert _encode(value, ABase) == {"x": 5}
    assert ABase.model_validate({"x": 5}).x == 5


def test_a_plain_alias_beside_a_validation_alias_is_written_under_both():
    value = PS(X=1, yy=2)

    for sent in (_encode(value, PS), wire_models({"item": value})["item"]):
        assert sent == {"X": 1, "yy": 2}
        assert PS.model_validate(sent) == value


def test_a_base_that_reads_the_validation_alias_form_as_other_values_gets_what_it_got_before():
    """The client knows the declared class. Declaring MBBase, every dump is refused and the
    validation-alias form is read as other values (`a=89`, `b=0`), so the dump goes out and is
    refused, as before. Declaring MBSub, the form it reads back is sent."""
    value = MBSub.model_validate({"a": 38, "p": {"k": 4}, "b": 89}, by_name=True, by_alias=False)
    form = {"a": 89, "p": {"k": 4}, "q": {"k": 38}}

    to_base = _encode(value, MBBase)
    assert to_base == {"a": 38, "p": {"k": 4}, "b": 89}
    with pytest.raises(ValidationError):
        MBBase.model_validate(to_base)
    assert MBBase.model_validate(form).b == 0

    assert _encode(value, MBSub) == form
    assert MBSub.model_validate(form) == value


def test_a_call_without_an_annotation_withholds_a_form_some_class_reads_as_other_values():
    """`call_rpc` and `RpcProxy` cannot tell which class the handler declares. MBBase would read
    MBSub's validation-alias form as other values, so it is withheld; the alias dump left is read
    by MBSub with `a` at its default, and no class reads it as the argument, so the call is refused
    before sending."""
    value = MBSub.model_validate({"a": 38, "p": {"k": 4}, "b": 89}, by_name=True, by_alias=False)

    with pytest.raises(RpcValidationError) as refused:
        wire_models({"item": value})

    assert [(d["type"], d["loc"]) for d in refused.value.details] == [
        ("value_would_be_lost", ["a"])
    ]


def test_a_base_that_refused_every_dump_is_not_sent_a_form_it_reads_with_a_default():
    """ZBase refuses every dump of a ZSub (`z` is required at `zz.k`) and reads ZSub's
    validation-alias form with `y=0`. A handler declaring ZBase refuses the call, as before,
    rather than receiving `y=0` without a word."""
    value = ZSub(Y=2, zz={"k": 9})

    sent = wire_models({"item": value})["item"]
    assert sent == {"Y": 2, "z": 9}
    with pytest.raises(ValidationError):
        ZBase.model_validate(sent)
    assert ZBase.model_validate({"Y": 2, "zz": {"k": 9}}).y == 0


def test_a_base_that_refuses_the_validation_alias_form_keeps_the_dump_it_reads():
    """FBase reads `{"x": 5}` and refuses `{"xx": 5}`. Without an annotation the dump it reads is
    sent; the client sends each declared class the form that class reads."""
    value = FSub.model_validate({"xx": 5})

    assert wire_models({"item": value})["item"] == {"x": 5}
    assert _encode(value, FBase) == {"x": 5}
    assert FBase.model_validate({"x": 5}).x == 5
    assert _encode(value, FSub) == {"xx": 5}
    assert FSub.model_validate({"xx": 5}) == value


def test_the_client_sends_a_nested_model_the_form_its_declared_class_reads():
    """The annotation says the inner model is an ASub, so the client writes it under `xx`, which
    ASub reads; ABase's reading of `x` by name has no say. Without an annotation ABase keeps that
    form back, and HoldsASub reads the dump left with `item.x` at its default, so the call is
    refused before sending."""
    value = HoldsASub(item=ASub.model_validate({"xx": 5}))

    sent = _encode(value, HoldsASub)
    assert sent == {"item": {"xx": 5}}
    assert HoldsASub.model_validate(sent) == value
    with pytest.raises(RpcValidationError) as refused:
        wire_models({"value": value})
    assert [d["loc"] for d in refused.value.details] == [["item.x"]]


def test_a_base_that_reads_the_form_as_other_values_only_as_json_still_withholds_it():
    """The service reads a message in python mode and then in JSON mode, so a base that accepts the
    form only as JSON still reads it, with `y=0`; the call goes out as before and is refused."""
    value = StrictZSub(when=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), Y=2, zz={"k": 9})

    sent = wire_models({"item": value})["item"]
    assert sent == {"when": "2026-01-02T03:04:05Z", "Y": 2, "z": 9}
    with pytest.raises(ValidationError):
        read_python_then_json(sent, StrictZBase.model_validate, StrictZBase.model_validate_json)

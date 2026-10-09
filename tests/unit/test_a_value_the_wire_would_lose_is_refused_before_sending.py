"""Every path offers the same forms, and a value no form carries is refused before it is sent.

`ServiceClient._encode` offered the model's default dump and its alias dump, never the dump by field
name, so a `serialize_by_alias` model with a `serialization_alias`, read by its field name, reached the
handler as the field's default (and its required twin was refused); `call_rpc` delivered it. Every path
now offers the same candidate forms.

Some models have no form that reads back as the argument: an `AliasChoices` whose first member is
another field's name, an `AliasPath` whose head is another field's name. The fallback form then
delivers a field's default where the caller set a different value, which is the value lost rather than
normalised, and the call is refused before it is sent, naming the fields.
"""

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
)

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import nested_form, wire_models

pytestmark = pytest.mark.unit


class SerializedByAliasReadByName(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(0, serialization_alias="X")


class SerializedByAliasRequired(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(serialization_alias="X")


class SerializedByAliasWithASerializer(BaseModel):
    """The same, with a serializer, so no form is written a level at a time: the whole-value dump
    by field name is the form that carries it."""

    model_config = ConfigDict(serialize_by_alias=True)
    x: int = Field(0, serialization_alias="X")

    @field_serializer("x")
    def _unchanged(self, value: int) -> int:
        return value


class AliasOuterOverSerializedByAlias(BaseModel):
    """The outer is read only by alias, the inner only by field name though it writes its alias:
    the inner level needs its dump by field name inside an outer written by alias."""

    o: int = Field(alias="O")
    inner: SerializedByAliasReadByName


class ChoiceIsAnotherFieldsName(BaseModel):
    """`a` is read first from `b`, which is also a field: a dict carrying `b` hands `a` its value."""

    a: int = Field(0, validation_alias=AliasChoices("b", "a"))
    b: int = 0


class PathHeadIsAnotherFieldsName(BaseModel):
    """`x` is read from `p.a`, and `p` is also a field: no dict holds both values."""

    p: dict = {}
    x: int = Field(0, validation_alias=AliasPath("p", "a"))


class Bumped(BaseModel):
    """A normalising validator: it reads back changed in every form, and nothing it set is lost."""

    model_config = ConfigDict(populate_by_name=True)
    n: int = Field(0, alias="N")

    @field_validator("n")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1


class BumpedBehindAChoice(BaseModel):
    """Read only under `xx`, and normalised: the dumps lose it to the default, the validation-alias
    form carries it, read again as the validator makes it."""

    x: int = Field(0, validation_alias=AliasChoices("xx"))

    @field_validator("x")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1


def _encode(value, annotation):
    return ServiceClient._encode(object.__new__(ServiceClient), value, annotation)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(SerializedByAliasReadByName(x=5), id="with-a-default"),
        pytest.param(SerializedByAliasRequired(x=5), id="required"),
        pytest.param(SerializedByAliasWithASerializer(x=5), id="with-a-serializer"),
    ],
)
def test_a_generated_client_offers_the_dump_by_field_name_as_call_rpc_does(value):
    assert _encode(value, type(value)) == {"x": 5}
    assert wire_models({"item": value})["item"] == {"x": 5}


def test_a_level_written_a_level_at_a_time_is_offered_its_dump_by_field_name_too():
    value = AliasOuterOverSerializedByAlias(O=1, inner=SerializedByAliasReadByName(x=5))

    assert _encode(value, AliasOuterOverSerializedByAlias) == {"O": 1, "inner": {"x": 5}}
    assert wire_models({"item": value})["item"] == {"O": 1, "inner": {"x": 5}}


@pytest.mark.parametrize(
    ("value", "lost"),
    [
        pytest.param(
            ChoiceIsAnotherFieldsName.model_validate({"a": 3}), ["a"], id="choice-is-a-field-name"
        ),
        pytest.param(
            PathHeadIsAnotherFieldsName.model_validate({"p": {"a": 5}}).model_copy(
                update={"p": {"k": 1}}
            ),
            ["x"],
            id="path-head-is-a-field-name",
        ),
    ],
)
def test_a_value_no_form_carries_is_refused_before_sending_on_every_path(value, lost):
    for send in (lambda: _encode(value, type(value)), lambda: wire_models({"item": value})):
        with pytest.raises(RpcValidationError, match="refused before sending") as refused:
            send()
        assert [d["loc"][0] for d in refused.value.details] == lost
        assert {d["type"] for d in refused.value.details} == {"value_would_be_lost"}


def test_a_normalising_validator_is_sent_as_before_and_not_refused():
    value = Bumped(n=1)

    assert _encode(value, Bumped) == {"n": 2}
    assert wire_models({"item": value})["item"] == {"N": 2}


def test_a_value_set_to_its_default_is_not_counted_as_lost():
    value = ChoiceIsAnotherFieldsName.model_validate({"a": 0})

    assert _encode(value, ChoiceIsAnotherFieldsName) == {"a": 0, "b": 0}


def test_a_normalised_value_takes_the_form_that_carries_it_rather_than_a_refusal():
    value = BumpedBehindAChoice.model_validate({"xx": 5})

    assert value.x == 6
    assert wire_models({"item": value})["item"] == {"xx": 6}
    assert _encode(value, BumpedBehindAChoice) == {"xx": 6}


class CrossedChain(BaseModel):
    """`a` is read from "b", `b` from "c", and `c` by its name, which is also `b`'s alias."""

    a: int = Field(0, alias="b")
    b: int = Field(0, alias="c")
    c: int = 0


def test_a_value_that_would_arrive_on_another_field_is_refused_before_sending():
    """No form reads back as the argument. `call_rpc` would send the alias dump `{"b": 1, "c": 3}`,
    read with `b` holding `c`'s value; the client would send `{"a": 1, "b": 2, "c": 3}`, read with
    `a` holding `b`'s and `b` holding `c`'s. Each path refuses, naming the fields it would cross."""
    value = CrossedChain.model_validate({"a": 1, "b": 2, "c": 3}, by_name=True, by_alias=False)

    with pytest.raises(RpcValidationError) as on_the_wire:
        wire_models({"item": value})
    with pytest.raises(RpcValidationError) as from_the_client:
        ServiceClient._encode(None, value, CrossedChain)

    assert [(d["type"], d["loc"]) for d in on_the_wire.value.details] == [
        ("value_would_be_misread", ["b"])
    ]
    assert [(d["type"], d["loc"]) for d in from_the_client.value.details] == [
        ("value_would_be_misread", ["a"]),
        ("value_would_be_misread", ["b"]),
    ]
    assert "b read as c's value" in str(on_the_wire.value)


def test_fields_holding_equal_values_are_not_counted_as_crossed():
    """With `b` and `c` equal, `b` read from "c" holds what the caller set: nothing is crossed.

    The client judges each field of what it sends, so it is the client that would name `b` as
    crossed. On the wire the alias form reads back equal to the argument and is sent as it is."""
    value = CrossedChain.model_validate({"a": 1, "b": 3, "c": 3}, by_name=True, by_alias=False)

    sent = wire_models({"item": value})["item"]

    assert CrossedChain.model_validate(sent) == value
    assert ServiceClient._encode(None, value, CrossedChain) == {"b": 1, "c": 3}


class ReadUnderXX(BaseModel):
    x: int = Field(serialization_alias="X", validation_alias="xx")


class ByNameOnly(ReadUnderXX):
    model_config = ConfigDict(validate_by_name=True, validate_by_alias=False)


class SerializedByAlias(ReadUnderXX):
    model_config = ConfigDict(serialize_by_alias=True, populate_by_name=True)


class ReadsByNameFirst(ByNameOnly, SerializedByAlias):
    """Reads `x` by name only; its base ReadUnderXX reads it only under `xx`."""


def test_the_declared_annotation_chooses_the_form_not_the_instances_own_class():
    """The instance's own class reads every dump by name, ReadUnderXX reads none of them: the
    client, which knows the handler declares ReadUnderXX, sends the form ReadUnderXX reads."""
    sent = ServiceClient._encode(None, ReadsByNameFirst(x=1), ReadUnderXX)

    assert sent == {"xx": 1}
    assert ReadUnderXX.model_validate(sent).x == 1


class ReadFromP(BaseModel):
    z: int = Field(validation_alias="p")


class SerializedByAliasOverP(ReadFromP):
    model_config = ConfigDict(serialize_by_alias=True, populate_by_name=True)


class WritesP(SerializedByAliasOverP):
    """Writes `z` under its alias `p`, which ReadFromP reads; reads it by name as well."""

    z: int = Field(0, alias="p")
    x: str = Field("d", serialization_alias="X")


def test_a_base_keeps_the_dump_it_reads_when_only_the_instances_class_reads_the_dump_by_name():
    """The model's own dump writes `z` under `p`, which ReadFromP reads. The dump by field name is
    read by WritesP and refused by ReadFromP, so it does not replace the dump the base reads.

    The form is chosen against the declared class. The client's second choice, among forms that
    hold no dump by field name, would also arrive at this dump, so the client's result alone does
    not show the declared class was used; `nested_form` is asked directly."""
    sent = ServiceClient._encode(None, WritesP(p=9, x="v1"), ReadFromP)

    assert sent["p"] == 9
    assert ReadFromP.model_validate(sent).z == 9
    assert nested_form(
        WritesP(p=9, x="v1"), alias_first=False, extra_forms="caller checks", declared=ReadFromP
    ) == {"p": 9, "X": "v1"}


class ReadsYAtPK(BaseModel):
    y: int = Field(0, validation_alias=AliasPath("p", "k"))


class AddsP(ReadsYAtPK):
    """Requires `p`, read under `pp` or by name; `y` is read at `p.k`."""

    model_config = ConfigDict(serialize_by_alias=True, populate_by_name=True)
    p: dict = Field(serialization_alias="P", validation_alias="pp")


def test_a_dump_by_field_name_read_as_other_values_does_not_replace_a_refusal():
    """The model's own dump writes `p` under `P` and is refused (`p` is required). The dump by
    field name is accepted, with `y` read from `p.k` as 1 in place of 8, a value that is neither
    a default nor another field's. The client sends what it sent before, and the service refuses
    it, rather than delivering `y=1`."""
    value = AddsP.model_validate({"y": 8, "p": {"k": 1}}, by_name=True, by_alias=False)

    sent = ServiceClient._encode(None, value, AddsP)

    assert sent == {"y": 8, "P": {"k": 1}}
    with pytest.raises(ValidationError):
        AddsP.model_validate(sent)


class BumpedBesideM(BaseModel):
    n: int
    m: int = 0

    @field_validator("n")
    @classmethod
    def _bump(cls, v: int) -> int:
        return v + 1


def test_a_normalised_value_equal_to_another_fields_is_not_counted_as_crossed():
    """`n` holds 6 and reads back as 7, which is `m`'s value. That is what the validator makes of
    the caller's `n`, not `m` crossing over, so the call goes out as before."""
    value = BumpedBesideM.model_validate({"n": 5, "m": 7})

    assert ServiceClient._encode(None, value, BumpedBesideM) == {"n": 6, "m": 7}
    assert wire_models({"item": value})["item"] == {"n": 6, "m": 7}


class NormalisingBase(BaseModel):
    n: int

    @field_validator("n")
    @classmethod
    def _bump(cls, v: int) -> int:
        return v + 1


class ChainOverANormalisingBase(NormalisingBase):
    a: int = Field(0, alias="b")
    b: int = Field(0, alias="c")
    c: int = 0


def test_a_base_that_reads_the_form_as_its_validators_make_of_it_keeps_the_call():
    """Without an annotation the handler may declare NormalisingBase, which reads every form as its
    validator makes of the caller's `n`: the call is not refused for it, though the subclass would
    read `b` holding `c`'s value, as it did before."""
    value = ChainOverANormalisingBase.model_validate(
        {"n": 5, "a": 1, "b": 2, "c": 3}, by_name=True, by_alias=False
    )

    sent = wire_models({"item": value})["item"]

    assert NormalisingBase.model_validate(sent).n == 7


class HoldsX(BaseModel):
    x: int


class HoldsXY(BaseModel):
    x: int
    y: int


class NestedUnderAnotherFieldsName(BaseModel):
    """`a` is read from "b", which is also `b`'s name: every form reads `a` from `b`'s model."""

    a: HoldsX = Field(alias="b")
    b: HoldsXY


def test_a_nested_model_that_would_arrive_holding_another_fields_values_is_refused():
    value = NestedUnderAnotherFieldsName.model_validate(
        {"a": {"x": 1}, "b": {"x": 52, "y": 13}}, by_name=True, by_alias=False
    )

    with pytest.raises(RpcValidationError) as on_the_wire:
        wire_models({"item": value})
    with pytest.raises(RpcValidationError) as from_the_client:
        ServiceClient._encode(None, value, NestedUnderAnotherFieldsName)

    for refused in (on_the_wire, from_the_client):
        assert [(d["type"], d["loc"]) for d in refused.value.details] == [
            ("value_would_be_misread", ["a"])
        ]

"""A model inside another keeps the form its own config wrote, and is sent, not refused.

The form written one model at a time (`nested_form`) has each level write its own fields under its
own config. A level above then writes its fields, and puts back each model below it as that model
wrote itself, wherever it sits: in a field, a dict or a subclass of one, a list, a tuple, a set or a
frozenset, and under the key the level's dump uses for the field (its name, or its alias when the
level is written by alias or its config serializes by alias). So a NaN that a model with
`ser_json_inf_nan="constants"` keeps is not turned into None by a level whose config writes null,
and a value whose only readable form is this one is delivered on the wire and by a generated
client. KV stores the literal exactly where Pydantic's own dump writes it, and refuses a value
where that dump writes null.
"""

from __future__ import annotations

import collections
import dataclasses
import math
from typing import Any

import pytest
from cliffracer_kv import ModelDoesNotReadBackError
from cliffracer_kv.serialization import deserialize_value, serialize_value
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from cliffracer.client import ServiceClient
from cliffracer.core.validation import nested_form, read_python_then_json, wire_models

pytestmark = pytest.mark.unit

CONSTANTS = ConfigDict(ser_json_inf_nan="constants")
NAN = float("nan")


class Keeps(BaseModel):
    model_config = CONSTANTS

    f: float = 0.0


class FrozenKeeps(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants", frozen=True)

    f: float = 0.0


class Writes(BaseModel):
    f: float = 0.0


def _outer(name: str, annotation: Any, default: Any, **field: Any) -> type[BaseModel]:
    """A default-config model whose `k` only its validation alias reads, so neither plain dump
    reads back and the form written one model at a time is the one sent, holding `inner`."""
    return type(
        name,
        (BaseModel,),
        {
            "__annotations__": {"k": int, "inner": annotation},
            "k": Field(0, validation_alias=AliasChoices("kk")),
            "inner": Field(default, **field),
        },
    )


InField = _outer("InField", Keeps, None)
InDict = _outer("InDict", dict[str, Keeps], None)
InOrderedDict = _outer("InOrderedDict", collections.OrderedDict[str, Keeps], None)
InList = _outer("InList", list[Keeps], None)
InTuple = _outer("InTuple", tuple[Keeps, ...], None)
InSet = _outer("InSet", set[FrozenKeeps], None)
InFrozenset = _outer("InFrozenset", frozenset[FrozenKeeps], None)


class ReadByAlias(BaseModel):
    """A level that reads its field only by its alias, so its dump by alias is the form chosen."""

    inner: Keeps = Field(default_factory=Keeps, alias="I")


class SerializedByAlias(BaseModel):
    """A level whose own default dump writes its aliases."""

    model_config = ConfigDict(serialize_by_alias=True)

    inner: Keeps = Field(default_factory=Keeps, alias="I")


UnderAnAliasReadByAlias = _outer("UnderAnAliasReadByAlias", ReadByAlias, None)
UnderAnAliasSerializedByAlias = _outer("UnderAnAliasSerializedByAlias", SerializedByAlias, None)


CASES = {
    "a-field": InField.model_validate({"kk": 1, "inner": Keeps(f=NAN)}),
    "a-dict": InDict.model_validate({"kk": 1, "inner": {"a": Keeps(f=NAN)}}),
    "a-dict-subclass": InOrderedDict.model_validate(
        {"kk": 1, "inner": collections.OrderedDict(a=Keeps(f=1.0), b=Keeps(f=NAN))}
    ),
    "a-list": InList.model_validate({"kk": 1, "inner": [Keeps(f=1.0), Keeps(f=NAN)]}),
    "a-tuple": InTuple.model_validate({"kk": 1, "inner": (Keeps(f=NAN),)}),
    "a-set": InSet.model_validate({"kk": 1, "inner": {FrozenKeeps(f=NAN)}}),
    "a-frozenset": InFrozenset.model_validate({"kk": 1, "inner": frozenset({FrozenKeeps(f=NAN)})}),
    "under-an-alias-written-by-alias": UnderAnAliasReadByAlias.model_validate(
        {"kk": 1, "inner": {"I": Keeps(f=NAN)}}
    ),
    "under-an-alias-serialized-by-alias": UnderAnAliasSerializedByAlias.model_validate(
        {"kk": 1, "inner": {"I": Keeps(f=NAN)}}
    ),
}


class NoConnection:
    pass


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return a == b or (a != a and b != b)
    if isinstance(a, BaseModel):
        return type(a) is type(b) and all(
            _same(getattr(a, n), getattr(b, n)) for n in type(a).model_fields
        )
    if dataclasses.is_dataclass(a) and not isinstance(a, type):
        return type(a) is type(b) and all(
            _same(getattr(a, f.name), getattr(b, f.name)) for f in dataclasses.fields(a)
        )
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list | tuple):
        return isinstance(b, list | tuple) and len(a) == len(b) and all(map(_same, a, b))
    if isinstance(a, set | frozenset):
        return (
            isinstance(b, set | frozenset)
            and len(a) == len(b)
            and all(any(_same(x, y) for y in b) for x in a)
        )
    return bool(a == b)


def _read(model: type[BaseModel], sent: Any) -> BaseModel:
    return read_python_then_json(sent, model.model_validate, model.model_validate_json)


@pytest.mark.parametrize("case", list(CASES))
def test_a_constants_model_below_a_default_one_is_sent_on_the_wire_with_its_nan(case):
    value = CASES[case]

    assert _same(_read(type(value), wire_models(value)), value)


@pytest.mark.parametrize("case", list(CASES))
def test_a_constants_model_below_a_default_one_is_sent_by_a_generated_client_with_its_nan(case):
    value = CASES[case]
    client = ServiceClient(NoConnection(), service="s", verify=False)

    assert _same(_read(type(value), client._encode(value, type(value))), value)


#: A dict subclass is left out: Pydantic's own dump writes a model inside one under the outermost
#: config, so its NaN is null there and KV refuses it (below).
STORABLE = [case for case in CASES if case != "a-dict-subclass"]


@pytest.mark.parametrize("case", STORABLE)
def test_a_constants_model_below_a_default_one_is_stored_with_the_literal_its_dump_writes(case):
    value = CASES[case]

    stored = serialize_value(value)

    assert stored.count(b"NaN") == value.model_dump_json().count("NaN") >= 1
    assert _same(deserialize_value(stored, as_type=type(value)), value)


def test_kv_refuses_a_model_in_a_dict_subclass_whose_nan_pydantic_writes_as_null():
    """Pydantic writes a model held by a dict subclass under the outermost model's config, so the
    model's own dump writes its NaN as null, and KV has no literal to store."""
    value = CASES["a-dict-subclass"]
    assert value.model_dump_json() == '{"k":1,"inner":{"a":{"f":1.0},"b":{"f":null}}}'

    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(value)


def test_a_default_model_holding_nan_below_another_is_sent_on_the_wire_and_by_a_client():
    """The wire and a client send Python values, which carry a NaN whatever a model's config."""
    value = _outer("HoldsAWriter", Writes, None).model_validate({"kk": 1, "inner": Writes(f=NAN)})
    client = ServiceClient(NoConnection(), service="s", verify=False)

    assert _same(_read(type(value), wire_models(value)), value)
    assert _same(_read(type(value), client._encode(value, type(value))), value)


def test_CONTROL_kv_refuses_a_nan_its_owners_dump_writes_as_null():
    """KV stores JSON text: a NaN the owning model's dump writes as null has no literal there."""
    value = _outer("StoresAWriter", Writes, None).model_validate({"kk": 1, "inner": Writes(f=NAN)})
    assert value.model_dump_json() == '{"k":1,"inner":{"f":null}}'

    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(value)


def test_CONTROL_an_excluded_field_holding_a_model_is_not_written():
    class SkipsOne(BaseModel):
        k: int = Field(0, validation_alias=AliasChoices("kk"))
        inner: Keeps = Field(default_factory=Keeps)
        hidden: Keeps = Field(default_factory=Keeps, exclude=True)

    value = SkipsOne.model_validate({"kk": 1, "inner": Keeps(f=NAN)})

    sent = wire_models(value)

    assert "hidden" not in sent and math.isnan(sent["inner"]["f"])


@pytest.mark.parametrize(
    ("value", "alias_first"),
    [
        (ReadByAlias.model_validate({"I": Keeps(f=NAN)}), True),
        (SerializedByAlias.model_validate({"I": Keeps(f=NAN)}), False),
    ],
    ids=["written-by-alias", "its-config-serializes-by-alias"],
)
def test_a_level_written_under_its_aliases_keeps_the_model_below_it(value, alias_first):
    """With the dumps alone (`extra_forms="none"`, as a client chooses again), the level's dump
    under its aliases is the form: the model below it is put back under the alias key."""
    form = nested_form(value, alias_first=alias_first, extra_forms="none")

    assert list(form) == ["I"] and math.isnan(form["I"]["f"])

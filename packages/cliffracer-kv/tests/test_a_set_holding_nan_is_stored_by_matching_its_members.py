"""A set holding a NaN, or a frozen model that holds one, is stored and reads back the same.

KV judges a read-back by declared type. A set finds its members by hash, and a NaN hashes by
identity, so a NaN read back is never found in the set written, nor is a frozen model holding one.
A typed set or frozenset is compared by matching each member read back to one written, one to one
under the item type, NaN equal to NaN, as a list's items are. A NaN that the model's own dump
writes as null still does not read back, and is refused.
"""

import math

import pytest
from cliffracer_kv.errors import ModelDoesNotReadBackError
from cliffracer_kv.serialization import deserialize_value, serialize_value
from pydantic import BaseModel, ConfigDict, field_validator

pytestmark = pytest.mark.unit

CONSTANTS = ConfigDict(ser_json_inf_nan="constants")


class FK(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants", frozen=True)
    f: float = 0.0


class Models(BaseModel):
    model_config = CONSTANTS
    s: set[FK] = set()


class FrozenModels(BaseModel):
    model_config = CONSTANTS
    s: frozenset[FK] = frozenset()


class Floats(BaseModel):
    model_config = CONSTANTS
    s: set[float] = set()


def _members(value) -> list[float]:
    return sorted((m.f if isinstance(m, BaseModel) else m for m in value.s), key=repr)


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (Models(s={FK(f=math.nan)}), b'{"s":[{"f":NaN}]}'),
        (FrozenModels(s=frozenset({FK(f=math.nan)})), b'{"s":[{"f":NaN}]}'),
        (Floats(s={math.nan}), b'{"s":[NaN]}'),
    ],
    ids=["set-of-frozen-models", "frozenset-of-frozen-models", "set-of-floats"],
)
def test_a_set_holding_nan_is_stored_and_reads_back(value, stored):
    assert serialize_value(value) == stored
    back = deserialize_value(stored, as_type=type(value))
    assert all(math.isnan(m) for m in _members(back)) and len(back.s) == len(value.s)


def test_a_set_mixing_nan_and_numbers_matches_each_member():
    value = Models(s={FK(f=math.nan), FK(f=1.0), FK(f=math.inf)})

    back = deserialize_value(serialize_value(value), as_type=Models)

    assert sorted(m.f for m in back.s if not math.isnan(m.f)) == [1.0, math.inf]
    assert sum(math.isnan(m.f) for m in back.s) == 1


class Nulls(BaseModel):
    s: set[float] = set()  # the default config writes NaN as null


def test_CONTROL_a_set_whose_nan_is_written_as_null_is_refused():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(Nulls(s={math.nan}))


class Halves(BaseModel):
    """A member read back is half the one written: no member read back matches."""

    model_config = CONSTANTS
    s: set[float] = set()

    @field_validator("s", mode="after")
    @classmethod
    def _halve_on_read(cls, value: set[float], info) -> set[float]:
        return {m / 2 for m in value} if info.mode == "json" else value


def test_a_set_whose_member_reads_back_changed_is_refused():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(Halves(s={4.0, math.nan}))


class Collapses(BaseModel):
    """Every member reads back as 1.0, so the set read back has fewer members."""

    model_config = CONSTANTS
    s: set[float] = set()

    @field_validator("s", mode="after")
    @classmethod
    def _collapse_on_read(cls, value: set[float], info) -> set[float]:
        return {1.0 for _ in value} if info.mode == "json" else value


def test_a_set_that_reads_back_with_fewer_members_is_refused():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(Collapses(s={1.0, 2.0}))


class AllNan(BaseModel):
    """Every member reads back as a NaN of its own, so two NaNs are read for one written."""

    model_config = CONSTANTS
    s: set[float] = set()

    @field_validator("s", mode="after")
    @classmethod
    def _nan_on_read(cls, value: set[float], info) -> set[float]:
        return {float("nan") for _ in value} if info.mode == "json" else value


def test_a_member_written_once_matches_only_one_member_read():
    with pytest.raises(ModelDoesNotReadBackError):
        serialize_value(AllNan(s={math.nan, 5.0}))

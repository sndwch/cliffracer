"""A model is written to KV in a form its own class reads back as the same model, or not at all.

`get(as_type=Model)` reads the stored bytes with `Model.model_validate_json`. A model was written
under its field names whenever its class accepted them, and accepting is not reading back the same
values: a field read only through `AliasChoices` or `AliasPath` naming other keys took its default,
two fields whose aliases are each other's names came back swapped, and a serializer that changes the
value stored the changed value. Nothing reported any of it.

The write now tries the field names, then the aliases, then each field where its validation alias
reads it, one model at a time (`nested_form`), and stores the first form the class reads back equal
to the model (`choose_wire_form`, as the RPC client chooses a form). When none does, `put`, `create`
and `put_object` raise `ModelDoesNotReadBackError`, a `KvError` and a `TypeError`, naming the model
and what each form read back as, and nothing reaches the bucket.
"""

from __future__ import annotations

import dataclasses
import json
import math
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any
from unittest.mock import AsyncMock
from uuid import UUID

import pydantic.dataclasses
import pytest
import typing_extensions
from cliffracer_kv import KvError, KvExtension, ModelDoesNotReadBackError
from cliffracer_kv.serialization import deserialize_value, serialize_value
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_serializer,
    model_validator,
)

pytestmark = pytest.mark.unit


class ChoicesElsewhere(BaseModel):
    a: int = Field(0, validation_alias=AliasChoices("x", "y"))


class PathElsewhere(BaseModel):
    a: int = Field(0, validation_alias=AliasPath("p", "a"))


class Crossed(BaseModel):
    """Each field's alias is the other field's name: by name the values are read swapped."""

    model_config = ConfigDict(populate_by_name=True)

    a: int = Field(0, alias="b")
    b: int = Field(0, alias="a")


#: a=5, b=7. Built from its aliases, since the constructor reads `a=` as field `b`'s alias.
CROSSED = Crossed.model_validate({"b": 5, "a": 7})


class ByNameOnly(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)

    item_id: int = Field(alias="itemId")


class AliasOnly(BaseModel):
    item_id: int = Field(alias="itemId")


class Nested(BaseModel):
    child: AliasOnly
    items: list[AliasOnly] = []


class MixedTree(BaseModel):
    """The parent is read by field name only, the child by alias only: no single form reads both."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)

    child_ref: AliasOnly = Field(alias="childRef")


class Shouting(BaseModel):
    name: str

    @field_serializer("name")
    def _upper(self, value: str) -> str:
        return value.upper()


class StrictTimes(BaseModel):
    model_config = ConfigDict(strict=True)

    when: datetime
    uid: UUID


class StrictTimesWithABeforeValidator(BaseModel):
    """Pydantic's JSON mode refuses the ISO string for a strict datetime behind a `before`
    validator, so this model's own dump is not readable by `get`."""

    model_config = ConfigDict(strict=True)

    when: datetime

    @model_validator(mode="before")
    @classmethod
    def _passthrough(cls, data):
        return data


WHEN = datetime(2026, 1, 1, tzinfo=UTC)

#: Read back changed whatever form they are written in.
REFUSED = [
    pytest.param(Shouting(name="a"), id="serializer-changes-the-value"),
    pytest.param(StrictTimesWithABeforeValidator(when=WHEN), id="strict-with-before-validator"),
]

#: Neither the field-name form nor the alias form reads back as the model: each is stored with its
#: fields where their validation aliases read them, one model at a time.
UNDER_VALIDATION_ALIASES = [
    pytest.param(ChoicesElsewhere(x=5), b'{"x":5}', id="alias-choices-naming-other-keys"),
    pytest.param(PathElsewhere(p={"a": 5}), b'{"p":{"a":5}}', id="alias-path"),
    pytest.param(
        MixedTree(child_ref=AliasOnly(itemId=1)),
        b'{"child_ref":{"itemId":1}}',
        id="tree-needing-two-forms",
    ),
]

READ_BACK = [
    pytest.param(CROSSED, id="crossed-aliases"),
    pytest.param(ByNameOnly(item_id=1), id="by-name-only"),
    pytest.param(AliasOnly(itemId=1), id="alias-only"),
    pytest.param(Nested(child=AliasOnly(itemId=1), items=[AliasOnly(itemId=2)]), id="nested"),
    pytest.param(StrictTimes(when=WHEN, uid=UUID(int=1)), id="strict"),
]


@pytest.mark.parametrize("value", READ_BACK)
def test_a_model_reads_back_as_itself(value):
    assert deserialize_value(serialize_value(value), as_type=type(value)) == value


def test_crossed_aliases_are_stored_under_the_aliases_the_class_reads_back_equal():
    assert (CROSSED.a, CROSSED.b) == (5, 7)

    stored = serialize_value(CROSSED)

    assert json.loads(stored) == {"b": 5, "a": 7}
    back = deserialize_value(stored, as_type=Crossed)
    assert (back.a, back.b) == (5, 7)


def test_CONTROL_crossed_aliases_stored_under_the_field_names_read_back_swapped():
    """The field-name form is accepted, so "accepted" alone would store it."""
    by_name = CROSSED.model_dump_json().encode()

    back = deserialize_value(by_name, as_type=Crossed)
    assert (back.a, back.b) == (7, 5)


@pytest.mark.parametrize("value", REFUSED)
def test_a_model_its_class_reads_back_from_no_form_is_refused(value):
    with pytest.raises(ModelDoesNotReadBackError, match=type(value).__name__):
        serialize_value(value)


@pytest.mark.parametrize(("value", "form"), UNDER_VALIDATION_ALIASES)
def test_a_model_read_through_its_validation_aliases_is_stored_where_they_read(value, form):
    stored = serialize_value(value)

    assert stored == form
    assert deserialize_value(stored, as_type=type(value)) == value


@pytest.mark.parametrize(("value", "form"), UNDER_VALIDATION_ALIASES)
def test_CONTROL_neither_dump_of_such_a_model_reads_back_as_it(value, form):
    for dump in (value.model_dump_json(), value.model_dump_json(by_alias=True)):
        try:
            back = type(value).model_validate_json(dump)
        except ValueError:
            continue
        assert back != value, dump


class ReadByName(BaseModel):
    x: int = 0


class ReadByChoice(ReadByName):
    x: int = Field(0, validation_alias=AliasChoices("xx"))


def test_a_validation_alias_form_a_base_class_reads_as_its_default_is_refused():
    """`get(as_type=ReadByName)` would read `{"xx": 5}` as x=0, so it is not stored."""
    with pytest.raises(ModelDoesNotReadBackError) as caught:
        serialize_value(ReadByChoice(xx=5))

    text = str(caught.value)
    assert 'under its validation aliases {"xx":5} reads as ReadByChoice(x=5)' in text, text
    assert "while ReadByName reads as ReadByName(x=0)" in text, text


def test_CONTROL_the_base_class_reads_the_validation_alias_form_as_its_default():
    assert ReadByChoice.model_validate_json('{"xx":5}').x == 5
    assert ReadByName.model_validate_json('{"xx":5}').x == 0


class ReadOnlyThroughZ(BaseModel):
    model_config = ConfigDict(extra="forbid")

    y: int = Field(validation_alias="z")


class AddsARequiredField(ReadOnlyThroughZ):
    x: int


def test_a_validation_alias_form_the_base_cannot_read_is_stored_where_it_read_no_form_before():
    """The leaf reads `y` only through `z`, so it reads neither dump back; the validation-alias
    form `{"x": 2, "z": 3}` it reads back. The base refuses that form (it forbids the extra `x`)
    and refused the dump earlier releases stored too (`y` is missing there), so storing it takes no
    read away from the base: the form is stored by the last of the three steps."""
    value = AddsARequiredField.model_validate({"z": 3, "x": 2})
    for dump in (value.model_dump_json(), value.model_dump_json(by_alias=True)):
        with pytest.raises(ValueError):
            AddsARequiredField.model_validate_json(dump)
        with pytest.raises(ValueError):
            ReadOnlyThroughZ.model_validate_json(dump)

    stored = serialize_value(value)

    assert json.loads(stored) == {"x": 2, "z": 3}
    assert deserialize_value(stored, as_type=AddsARequiredField) == value
    with pytest.raises(ValueError):
        ReadOnlyThroughZ.model_validate_json(stored)


class FloatAtAChoice(BaseModel):
    f: float = Field(0.0, validation_alias=AliasChoices("ff"))


class FloatAtAChoiceWrittenAsConstants(FloatAtAChoice):
    model_config = ConfigDict(ser_json_inf_nan="constants")


@pytest.mark.parametrize("number", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_a_validation_alias_form_holding_a_nan_or_an_infinity_is_not_written(number):
    """JSON has no number for these, and the model writes them as null, not as the `NaN` or
    `Infinity` literal; the validation-alias form would write the literal, so it is not offered and
    the model, which reads neither dump back, is refused."""
    value = FloatAtAChoice.model_validate({"ff": number})

    with pytest.raises(ModelDoesNotReadBackError, match="FloatAtAChoice"):
        serialize_value(value)


@pytest.mark.parametrize(
    ("number", "literal"),
    [(float("nan"), "NaN"), (float("inf"), "Infinity")],
    ids=["nan", "inf"],
)
def test_a_model_that_writes_constants_is_stored_with_the_literal_and_reads_back(number, literal):
    value = FloatAtAChoiceWrittenAsConstants.model_validate({"ff": number})

    stored = serialize_value(value)

    assert stored == f'{{"ff":{literal}}}'.encode()
    back = deserialize_value(stored, as_type=FloatAtAChoiceWrittenAsConstants).f
    assert math.isnan(back) if math.isnan(number) else back == number


def test_the_refusal_is_a_kv_error_and_a_type_error_and_says_what_each_form_read_back_as():
    with pytest.raises(ModelDoesNotReadBackError) as caught:
        serialize_value(Shouting(name="a"))

    assert isinstance(caught.value, KvError) and isinstance(caught.value, TypeError)
    text = str(caught.value)
    assert "field names" in text and "aliases" in text
    assert "Shouting(name='A')" in text, text


def test_a_model_that_read_back_under_its_field_names_is_stored_byte_for_byte_as_before():
    class Plain(BaseModel):
        user_id: str
        count: int = 0

    assert serialize_value(Plain(user_id="u1", count=2)) == b'{"user_id":"u1","count":2}'


def _extension() -> tuple[KvExtension, AsyncMock, AsyncMock]:
    bucket = AsyncMock()
    store = AsyncMock()
    extension = KvExtension(object_stores=["media"])
    extension.get_bucket = AsyncMock(return_value=bucket)  # type: ignore[method-assign]
    extension.get_object_store = AsyncMock(return_value=store)  # type: ignore[method-assign]
    extension._message_ttl = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return extension, bucket, store


@pytest.mark.parametrize(
    "write",
    [
        lambda ext, value: ext.put("b", "k", value),
        lambda ext, value: ext.put("b", "k", value, revision=3),
        lambda ext, value: ext.create("b", "k", value),
        lambda ext, value: ext.put_object("media", "o", value),
    ],
    ids=["put", "put-with-revision", "create", "put_object"],
)
async def test_every_write_refuses_the_model_and_sends_nothing(write):
    extension, bucket, store = _extension()

    with pytest.raises(ModelDoesNotReadBackError):
        await write(extension, Shouting(name="a"))

    bucket.put.assert_not_awaited()
    bucket.update.assert_not_awaited()
    bucket.create.assert_not_awaited()
    store.put.assert_not_awaited()


async def test_put_stores_the_form_get_reads_back_as_the_model():
    extension, bucket, _ = _extension()
    bucket.put.return_value = 1

    await extension.put("b", "k", CROSSED)

    stored = bucket.put.await_args.args[1]
    assert deserialize_value(stored, as_type=Crossed) == CROSSED


# --- what "reads back as itself" compares: what is stored, field by field ---------------------------


class WithPrivate(BaseModel):
    name: str
    _cache: dict = PrivateAttr(default_factory=dict)


class NanConstants(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")

    ratio: float


class StrictAliasOnlyTime(BaseModel):
    """Read only by its alias, and strict: python mode refuses the ISO string JSON writes."""

    model_config = ConfigDict(strict=True)

    when: datetime = Field(alias="When")


class AnyPayload(BaseModel):
    payload: Any


class ExtraHoldsAModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str


class DateOnly(BaseModel):
    """A typed datetime whose serializer drops the time: it reads back as another datetime."""

    when: datetime

    @field_serializer("when")
    def _date_only(self, value: datetime) -> str:
        return value.date().isoformat()


def _with_private() -> WithPrivate:
    value = WithPrivate(name="a")
    value._cache["k"] = 1
    return value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_with_private(), id="private-attribute-set-at-runtime"),
        pytest.param(NanConstants(ratio=float("nan")), id="nan-written-as-a-constant"),
        pytest.param(StrictAliasOnlyTime(When=WHEN), id="strict-alias-only-datetime"),
        pytest.param(
            AnyPayload(payload={"when": WHEN, "pair": (1, 2)}), id="any-holding-a-datetime"
        ),
        pytest.param(
            ExtraHoldsAModel(name="a", child=AliasOnly(itemId=1)), id="extra-holding-a-model"
        ),
    ],
)
def test_a_model_whose_stored_fields_read_back_is_stored(value):
    """Only what is stored is compared: not a private attribute, a NaN as NaN, and a field typed
    `Any` or an extra by its JSON form, which is all it promised."""
    stored = serialize_value(value)

    back = deserialize_value(stored, as_type=type(value))
    assert json.loads(back.model_dump_json(by_alias=True)) == json.loads(stored) or (
        isinstance(value, NanConstants) and back.ratio != back.ratio
    )


def test_a_strict_alias_only_datetime_is_stored_under_the_alias_json_mode_reads():
    stored = serialize_value(StrictAliasOnlyTime(When=WHEN))

    assert json.loads(stored) == {"When": "2026-01-01T00:00:00Z"}
    assert deserialize_value(stored, as_type=StrictAliasOnlyTime).when == WHEN


def test_a_typed_field_that_reads_back_as_another_value_is_refused():
    with pytest.raises(ModelDoesNotReadBackError, match="DateOnly"):
        serialize_value(DateOnly(when=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)))


class CrossedWithPrivate(Crossed):
    _seen: int = PrivateAttr(default=0)


def test_the_form_is_chosen_by_what_is_stored_not_by_a_private_attribute():
    """Model `==` compares private attributes, so with one set neither form is `==`; the choice
    still takes the alias form, the one whose stored fields read back."""
    value = CrossedWithPrivate.model_validate({"b": 5, "a": 7})
    value._seen = 3

    stored = serialize_value(value)

    assert json.loads(stored) == {"b": 5, "a": 7}


# --- a position declared Any, at any depth, promises its JSON form only --------------------------


class DictOfAny(BaseModel):
    d: dict[str, Any]


class ListOfAny(BaseModel):
    items: list[Any]


class TupleOfAny(BaseModel):
    t: tuple[Any, ...]


class OptionalDictOfAny(BaseModel):
    d: dict[str, Any] | None = None


class DictOfDatetimeDateOnly(BaseModel):
    """A typed position: a value its serializer changes reads back as another datetime."""

    d: dict[str, datetime]

    @field_serializer("d")
    def _dates_only(self, value: dict[str, datetime]) -> dict[str, str]:
        return {key: when.date().isoformat() for key, when in value.items()}


class AnyHoldsAModel(BaseModel):
    payload: Any


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(DictOfAny(d={"when": WHEN, "id": UUID(int=1)}), id="dict-of-any"),
        pytest.param(ListOfAny(items=[WHEN, (1, 2)]), id="list-of-any"),
        pytest.param(TupleOfAny(t=(WHEN, UUID(int=1))), id="tuple-of-any"),
        pytest.param(OptionalDictOfAny(d={"when": WHEN}), id="optional-dict-of-any"),
    ],
)
def test_a_value_at_a_position_declared_any_is_stored_and_reads_back_as_its_json(value):
    stored = serialize_value(value)

    back = deserialize_value(stored, as_type=type(value))
    assert json.loads(back.model_dump_json()) == json.loads(value.model_dump_json())


class Colour(Enum):
    RED = "red"


type Payload = dict[str, Any]
type Anything = Any
type Keyed[V] = dict[str, V]
type Shade = Colour | str
#: Not the class `type` makes: `typing_extensions` defines its own on these Pythons, so the
#: `type` keyword ruff suggests would test the other class.
OldStylePayload = typing_extensions.TypeAliasType("OldStylePayload", dict[str, Any])  # noqa: UP040


class AliasedPayloads(BaseModel):
    """Each position is `dict[str, Any]` or `Any` behind a type alias, at some depth."""

    payload: Payload
    maybe: Payload | None = None
    by_key: dict[str, Payload] = {}
    many: list[Payload] = []
    anything: Anything = None
    old_style: OldStylePayload = {}
    keyed: Keyed[Any] = {}


class Box[T](BaseModel):
    """`T` is unbound: an unparameterised `Box` validates it as `Any`."""

    item: T
    items: list[T] = []


class BoundToDict[T: dict](BaseModel):
    item: T


class DictOrList[T: (dict, list)](BaseModel):
    item: T


class IntOrAny(BaseModel):
    value: int | Any


class AliasedArms(BaseModel):
    """Union arms that declare `Any` through an alias."""

    payload: Payload | int = 0
    anything: Anything | int = 0


class Opaque:
    """A class pydantic has no schema for: an arm of this type cannot be checked."""


class IntOrOpaque(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    value: int | Opaque


class OpaqueOrAnything(BaseModel):
    """An arm that cannot be checked holds only an instance of its class."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    value: Opaque | Any
    entries: Opaque | dict[str, Any] = {}
    #: Parameterised, it holds what is an instance of its origin, `list`: not a dict.
    mapping: list[Opaque] | dict[str, Any] = {}


class LabelsOrAnything(BaseModel):
    """`Annotated` metadata that is a dict makes the arm unhashable, so it is built uncached."""

    labels: list[Annotated[str, {"source": "form"}]] | list[Any]


class IntsOrAnything(BaseModel):
    """A value no typed arm holds is judged by the arm that promises only its JSON."""

    d: dict[str, int] | dict[str, Any]


class DictOfAnnotatedAny(BaseModel):
    d: dict[str, Annotated[Any, Field(description="anything")]]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            AliasedPayloads(
                payload={"when": WHEN},
                maybe={"when": WHEN},
                by_key={"k": {"when": WHEN}},
                many=[{"when": WHEN}],
                anything=WHEN,
                old_style={"when": WHEN},
                keyed={"when": WHEN},
            ),
            id="type-alias-of-any",
        ),
        pytest.param(Box(item=WHEN, items=[WHEN, UUID(int=1)]), id="unbound-typevar"),
        pytest.param(BoundToDict(item={"when": WHEN}), id="typevar-bound-to-dict"),
        pytest.param(DictOrList(item={"when": WHEN}), id="typevar-constrained-to-dict-or-list"),
        pytest.param(IntOrAny(value=WHEN), id="union-with-an-any-arm"),
        pytest.param(AliasedArms(payload={"when": WHEN}, anything=WHEN), id="aliased-union-arms"),
        pytest.param(IntsOrAnything(d={"when": WHEN}), id="value-only-the-any-arm-holds"),
        pytest.param(IntOrOpaque(value=5), id="beside-an-arm-that-cannot-be-checked"),
        pytest.param(
            OpaqueOrAnything(value=WHEN, entries={"when": WHEN}, mapping={"when": WHEN}),
            id="not-held-by-an-arm-that-cannot-be-checked",
        ),
        pytest.param(LabelsOrAnything(labels=[WHEN]), id="beside-an-unhashable-arm"),
        pytest.param(DictOfAnnotatedAny(d={"when": WHEN}), id="dict-of-annotated-any"),
    ],
)
def test_a_position_that_declares_any_through_an_alias_a_typevar_or_annotated_is_any(value):
    stored = serialize_value(value)

    back = deserialize_value(stored, as_type=type(value))
    assert json.loads(back.model_dump_json()) == json.loads(value.model_dump_json())


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(OpaqueOrAnything(value=WHEN), id="not-held-by-an-arm-that-cannot-be-checked"),
        pytest.param(LabelsOrAnything(labels=[WHEN]), id="beside-an-unhashable-arm"),
        pytest.param(AliasedPayloads(payload={"when": WHEN}), id="type-alias-of-any"),
        pytest.param(Box(item=WHEN), id="unbound-typevar"),
        pytest.param(IntOrAny(value=WHEN), id="union-with-an-any-arm"),
        pytest.param(DictOfAnnotatedAny(d={"when": WHEN}), id="dict-of-annotated-any"),
    ],
)
def test_CONTROL_such_a_position_reads_back_another_value_than_was_written(value):
    """The datetime reads back as its ISO string: only "Any" makes that the same."""
    assert type(value).model_validate_json(value.model_dump_json()) != value


class ShadeBehindAnAlias(BaseModel):
    shade: Shade


class HoldsAShade[T: Colour | str](BaseModel):
    shade: T


class ColourOrTextVar[T: (Colour, str)](BaseModel):
    shade: T


class ShadesOrAnything(BaseModel):
    shades: dict[str, Colour] | dict[str, Any]


class Ledger(dict):
    """A dict subclass pydantic has no schema for: it reads back as a plain dict."""


class AnythingOrLedger(BaseModel):
    """An arm that cannot be checked holds the value too, so it is compared under that arm."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    entries: dict[str, Any] | Ledger


class PlainOrLedger(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    entries: dict[str, Any] | Ledger


class AnythingOrShade(BaseModel):
    """The arm that promises only JSON comes first."""

    shade: Any | Colour


class AnythingOrShades(BaseModel):
    shades: dict[str, Any] | dict[str, Colour]


class SetsOrAnything(BaseModel):
    groups: list[set[int]] | list[Any]


class KeyedShades(BaseModel):
    shades: Keyed[Colour | str]


class NumberOrText(BaseModel):
    """Holds a datetime when one is assigned or constructed past validation: no arm holds it."""

    value: int | str


class ShadeOrMapping(BaseModel):
    """A container arm compares by JSON only a value that is that container."""

    shade: Colour | str | dict


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(ShadeBehindAnAlias(shade=Colour.RED), id="type-alias"),
        pytest.param(HoldsAShade(shade=Colour.RED), id="typevar-bound"),
        pytest.param(ColourOrTextVar(shade=Colour.RED), id="typevar-constrained"),
        pytest.param(ShadeOrMapping(shade=Colour.RED), id="union-with-a-container-arm"),
        pytest.param(
            ShadesOrAnything(shades={"a": Colour.RED}), id="value-a-typed-arm-holds-beside-any"
        ),
        pytest.param(SetsOrAnything(groups=[{1, 2}]), id="set-a-typed-arm-holds-beside-any"),
        pytest.param(AnythingOrShade(shade=Colour.RED), id="value-a-typed-arm-holds-after-any"),
        pytest.param(
            AnythingOrLedger(entries=Ledger(when=WHEN)),
            id="value-an-unchecked-arm-holds-beside-any",
        ),
        pytest.param(
            AnythingOrShades(shades={"a": Colour.RED}),
            id="value-a-typed-arm-holds-after-a-dict-of-any",
        ),
        pytest.param(KeyedShades(shades={"a": Colour.RED}), id="generic-alias-arguments"),
        pytest.param(
            NumberOrText.model_construct(value=WHEN),
            id="value-no-arm-holds",
            # pydantic says the datetime is not the declared type, which is the case under test
            marks=pytest.mark.filterwarnings("ignore:Pydantic serializer warnings:UserWarning"),
        ),
    ],
)
def test_a_value_a_typed_position_holds_is_compared_by_value_wherever_it_is_declared(value):
    """Behind an alias, a TypeVar's bound or constraints, or beside an arm that promises only JSON,
    a value a typed position holds reads back as another value: `Colour.RED` as the string
    `"red"`, a set as a list."""
    with pytest.raises(ModelDoesNotReadBackError, match=type(value).__name__):
        serialize_value(value)


def test_a_value_at_a_typed_position_that_reads_back_changed_is_refused():
    with pytest.raises(ModelDoesNotReadBackError, match="DictOfDatetimeDateOnly"):
        serialize_value(
            DictOfDatetimeDateOnly(d={"when": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)})
        )


def test_a_model_held_by_an_any_field_is_stored_under_its_aliases():
    """Its JSON form is its alias dump, which only the alias form of the holder writes."""
    stored = serialize_value(AnyHoldsAModel(payload=AliasOnly(itemId=1)))

    assert json.loads(stored) == {"payload": {"itemId": 1}}


class ColourOrText(BaseModel):
    shade: Colour | str


class HoldsColour(BaseModel):
    inner: ColourOrText


def test_a_typed_position_is_compared_by_value_even_where_the_json_is_the_same():
    """`Colour | str` holding `Colour.RED` writes `"red"`, which reads back as the string: the same
    JSON, another value. The position is typed, so the value is compared and the model, held by a
    typed field of another, is refused."""
    value = HoldsColour(inner=ColourOrText(shade=Colour.RED))
    assert HoldsColour.model_validate_json(value.model_dump_json()).inner.shade == "red"

    with pytest.raises(ModelDoesNotReadBackError, match="HoldsColour"):
        serialize_value(value)


# --- a base class that declares the model's fields is a reader too --------------------------------


class Plain0(BaseModel):
    p: dict = {}


class AliasedLeaf(Plain0):
    """Redeclares `p` under an alias it reads only: the base reads `p`, the leaf reads `y`."""

    p: dict = Field({}, alias="y")


class PopulatableLeaf(Plain0):
    model_config = ConfigDict(validate_by_name=True)

    p: dict = Field({}, alias="y")


class AliasedBase(BaseModel):
    p: dict = Field({}, alias="q")


class PlainLeaf(AliasedBase):
    p: dict = {}


def test_a_form_every_declaring_class_reads_back_is_stored():
    """The leaf reads `p` by name too, so the field-name form serves the base and the leaf."""
    value = PopulatableLeaf.model_validate({"y": {"k": 5}})

    stored = serialize_value(value)

    assert json.loads(stored) == {"p": {"k": 5}}
    assert deserialize_value(stored, as_type=Plain0).p == {"k": 5}
    assert deserialize_value(stored, as_type=PopulatableLeaf).p == {"k": 5}


def test_a_leaf_whose_alias_form_a_base_reads_as_its_default_and_which_cannot_read_its_names_is_refused():
    """The leaf reads only `y`, so the field-name form, which earlier releases stored, reads back
    as `{}` through the leaf; the alias form reads back as `{}` through the base. No form reads
    back right through both, and storing the alias form would hand the base its default
    silently, so the write is refused."""
    value = AliasedLeaf.model_validate({"y": {"k": 5}})
    assert AliasedLeaf.model_validate_json(value.model_dump_json()).p == {}
    assert Plain0.model_validate_json(value.model_dump_json(by_alias=True)).p == {}

    with pytest.raises(ModelDoesNotReadBackError, match="AliasedLeaf"):
        serialize_value(value)


def test_where_no_form_serves_every_class_the_form_earlier_releases_stored_is_kept():
    """The base reads only `q`, so no form serves it; the leaf reads the field-name form, which
    earlier releases stored, so that form is stored and every class reads what it read before."""
    value = PlainLeaf(p={"k": 5})
    assert AliasedBase.model_validate_json(value.model_dump_json()).p == {}

    stored = serialize_value(value)

    assert stored == value.model_dump_json().encode()
    assert deserialize_value(stored, as_type=PlainLeaf).p == {"k": 5}


class StrictKeysBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p: dict = {}


class AliasOnlyOverStrictKeys(StrictKeysBase):
    """Ignores the keys it does not read, so it accepts the field-name form, as `p`'s default."""

    model_config = ConfigDict(extra="ignore")

    p: dict = Field({}, alias="y")


class QBase(BaseModel):
    p: dict = Field({}, alias="q")


class AliasOnlyOverQ(QBase):
    p: dict = Field({}, alias="y")


def test_a_form_that_would_cost_a_base_a_read_it_had_is_refused():
    """Earlier releases stored the field-name form, which the leaf accepts but reads as `p`'s
    default, and which the base reads right; the base refuses the alias form's extra key. Storing
    the alias form would take away the base's read, so the write is refused."""
    value = AliasOnlyOverStrictKeys.model_validate({"y": {"k": 5}})
    assert AliasOnlyOverStrictKeys.model_validate_json(value.model_dump_json()).p == {}
    assert StrictKeysBase.model_validate_json(value.model_dump_json()).p == {"k": 5}

    with pytest.raises(ModelDoesNotReadBackError, match="AliasOnlyOverStrictKeys"):
        serialize_value(value)


def test_a_form_a_base_would_read_as_another_value_is_refused():
    """The base reads only `q`, so it reads the alias form's `p` as its default; the leaf cannot
    read the field-name form back. No form is read right by both, so the write is refused."""
    value = AliasOnlyOverQ.model_validate({"y": {"k": 5}})
    assert QBase.model_validate_json(value.model_dump_json(by_alias=True)).p == {}

    with pytest.raises(ModelDoesNotReadBackError, match="AliasOnlyOverQ"):
        serialize_value(value)


class NeedsAPath(BaseModel):
    y: int = Field(0, alias="Y")
    p: dict = Field(validation_alias=AliasPath("pp", "k"))


class RenamesThePath(NeedsAPath):
    p: dict = Field({}, alias="z")


def test_a_form_a_base_cannot_read_where_it_could_not_read_the_earlier_form_is_stored():
    """The leaf reads `Y` and `z` only, so it reads the field-name form, which earlier releases
    stored, as its defaults; the base requires `pp.k` and can read neither form. Storing the alias
    form costs the base nothing it had: it refuses both, and nothing reads another value."""
    value = RenamesThePath.model_validate({"Y": 5, "z": {"k": 2}})
    names = value.model_dump_json()
    assert RenamesThePath.model_validate_json(names) != value
    for text in (names, value.model_dump_json(by_alias=True)):
        with pytest.raises(ValueError):
            NeedsAPath.model_validate_json(text)

    stored = serialize_value(value)

    assert json.loads(stored) == {"Y": 5, "z": {"k": 2}}
    assert deserialize_value(stored, as_type=RenamesThePath) == value


def test_a_plain_dict_beside_an_arm_that_cannot_be_checked_is_judged_by_the_dict_arm():
    """`Ledger` is a `dict` subclass, and a plain dict is not one, so only `dict[str, Any]` holds
    it and it is compared by its JSON form; a `Ledger` itself is compared by value (refused)."""
    value = PlainOrLedger(entries={"when": WHEN})

    stored = serialize_value(value)

    back = deserialize_value(stored, as_type=PlainOrLedger)
    assert json.loads(back.model_dump_json()) == json.loads(value.model_dump_json())


def _counting_nested_form(monkeypatch) -> list[object]:
    from cliffracer_kv import serialization

    calls: list[object] = []
    real = serialization.nested_form

    def counting(value, **kwargs):
        calls.append(value)
        return real(value, **kwargs)

    monkeypatch.setattr(serialization, "nested_form", counting)
    return calls


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(CROSSED, id="read-back-under-its-aliases"),
        pytest.param(AliasOnly(itemId=1), id="read-back-under-its-alias"),
        pytest.param(ByNameOnly(item_id=1), id="read-back-under-its-names"),
    ],
)
def test_the_validation_alias_form_is_not_built_when_a_dump_serves(monkeypatch, value):
    """Every write pays for each form it builds, so the third is built only once the dumps fail."""
    calls = _counting_nested_form(monkeypatch)

    serialize_value(value)

    assert calls == []


def test_CONTROL_the_validation_alias_form_is_built_when_no_dump_serves(monkeypatch):
    calls = _counting_nested_form(monkeypatch)

    serialize_value(ChoicesElsewhere(x=5))

    assert len(calls) == 1


class InnerWrittenAsNull(BaseModel):
    f: float = Field(0.0, validation_alias=AliasChoices("ff"))


class OuterWrittenAsConstants(BaseModel):
    """Writes constants, but the inner model owns `f` and writes a NaN there as null."""

    model_config = ConfigDict(ser_json_inf_nan="constants")

    i: InnerWrittenAsNull = Field(
        default_factory=InnerWrittenAsNull, validation_alias=AliasChoices("ii")
    )


class InnerWrittenAsConstants(InnerWrittenAsNull):
    model_config = ConfigDict(ser_json_inf_nan="constants")


class OuterAndInnerWrittenAsConstants(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")

    i: InnerWrittenAsConstants = Field(
        default_factory=InnerWrittenAsConstants, validation_alias=AliasChoices("ii")
    )


def test_a_nan_owned_by_a_model_that_writes_null_is_not_written_as_a_literal():
    """Each model writes the positions it owns under its own config, and the inner one writes a
    NaN as null: the outer model's "constants" does not license a literal there, so the
    validation-alias form is not offered and the value, which no dump reads back, is refused."""
    value = OuterWrittenAsConstants.model_validate({"ii": {"ff": float("nan")}})
    assert value.model_dump_json() == '{"i":{"f":null}}'

    with pytest.raises(ModelDoesNotReadBackError, match="OuterWrittenAsConstants"):
        serialize_value(value)


def test_CONTROL_the_same_tree_holding_a_finite_number_is_stored_where_its_aliases_read_it():
    value = OuterWrittenAsConstants.model_validate({"ii": {"ff": 2.0}})

    assert serialize_value(value) == b'{"ii":{"ff":2.0}}'


def test_a_nan_in_a_tree_whose_every_model_writes_constants_is_stored_with_the_literal():
    value = OuterAndInnerWrittenAsConstants.model_validate({"ii": {"ff": float("nan")}})
    assert value.model_dump_json() == '{"i":{"f":NaN}}'

    stored = serialize_value(value)

    assert stored == b'{"ii":{"ff":NaN}}'
    assert math.isnan(deserialize_value(stored, as_type=OuterAndInnerWrittenAsConstants).i.f)


class InnersWrittenAsNull(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")

    i: list[InnerWrittenAsNull] = Field(default_factory=list, validation_alias=AliasChoices("ii"))


class InnersWrittenAsConstants(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")

    i: list[InnerWrittenAsConstants] = Field(
        default_factory=list, validation_alias=AliasChoices("ii")
    )


def test_a_nan_owned_by_a_model_in_a_list_that_writes_null_is_not_written_as_a_literal():
    value = InnersWrittenAsNull.model_validate({"ii": [{"ff": 1.0}, {"ff": float("nan")}]})
    assert value.model_dump_json() == '{"i":[{"f":1.0},{"f":null}]}'

    with pytest.raises(ModelDoesNotReadBackError, match="InnersWrittenAsNull"):
        serialize_value(value)


def test_CONTROL_a_nan_owned_by_a_model_in_a_list_that_writes_constants_is_stored_with_the_literal():
    value = InnersWrittenAsConstants.model_validate({"ii": [{"ff": 1.0}, {"ff": float("nan")}]})
    assert value.model_dump_json() == '{"i":[{"f":1.0},{"f":NaN}]}'

    stored = serialize_value(value)

    assert stored == b'{"ii":[{"ff":1.0},{"ff":NaN}]}'
    assert math.isnan(deserialize_value(stored, as_type=InnersWrittenAsConstants).i[1].f)


@pydantic.dataclasses.dataclass
class PydanticDataclassWrittenAsNull:
    g: float = 0.0


@dataclasses.dataclass
class StdlibDataclass:
    g: float = 0.0


class HoldsAPydanticDataclass(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")

    f: float = Field(0.0, validation_alias=AliasChoices("ff"))
    d: PydanticDataclassWrittenAsNull = Field(default_factory=PydanticDataclassWrittenAsNull)


class HoldsAStdlibDataclass(BaseModel):
    model_config = ConfigDict(ser_json_inf_nan="constants")

    f: float = Field(0.0, validation_alias=AliasChoices("ff"))
    d: StdlibDataclass = Field(default_factory=StdlibDataclass)


def test_a_pydantic_dataclass_holding_no_nan_does_not_withhold_the_literal_from_the_model_owning_it():
    """The literal is written where the model owning the NaN writes one. Here the NaN is the outer
    model's, which writes it as the literal, and the Pydantic dataclass beside it, whose own config
    writes null, holds no NaN: it does not decide, and the value is stored with the literal where
    Pydantic's own dump puts it."""
    value = HoldsAPydanticDataclass.model_validate({"ff": float("nan"), "d": {"g": 1.0}})
    assert value.model_dump_json() == '{"f":NaN,"d":{"g":1.0}}'

    stored = serialize_value(value)

    assert stored == b'{"d":{"g":1.0},"ff":NaN}'
    assert math.isnan(deserialize_value(stored, as_type=HoldsAPydanticDataclass).f)


def test_CONTROL_a_stdlib_dataclass_has_no_config_and_does_not_withhold_the_literal():
    value = HoldsAStdlibDataclass.model_validate({"ff": float("nan"), "d": {"g": 1.0}})
    assert value.model_dump_json() == '{"f":NaN,"d":{"g":1.0}}'

    assert serialize_value(value) == b'{"d":{"g":1.0},"ff":NaN}'

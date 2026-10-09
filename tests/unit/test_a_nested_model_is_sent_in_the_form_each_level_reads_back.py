"""A model tree whose levels need different alias settings is sent one level at a time.

A whole-value dump has a single `by_alias` switch. An outer model read by field name that holds
an inner model read by alias has no whole-value form the service accepts: by name refuses the inner,
by alias refuses the outer. `nested_form` writes each model after the models inside it, so each
level takes the form its own class reads back, and it is offered to `choose_wire_form` after the
two whole-value forms, so a tree they serve is sent exactly as before.

It dumps a copy of each outer model that holds its nested models as dicts. A model that declares
something that runs on a dump (a `field_serializer`, a `model_serializer`, a `computed_field`) would
be handed that copy rather than the caller's model, so the form is not offered for a tree that has
one on the way, and the call goes out as it did.
"""

import math
import warnings
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    computed_field,
    create_model,
    field_serializer,
    field_validator,
    model_serializer,
)

from cliffracer.client import ServiceClient
from cliffracer.core.validation import wire_models

pytestmark = pytest.mark.unit


class Inner(BaseModel):
    inner_name: str = Field(alias="innerName")


class OuterByName(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    outer_name: str = Field(alias="outerName")
    inner: Inner


class OuterOfMany(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    inners: list[Inner] = Field(alias="innersX")
    by_key: dict[str, Inner] = Field(alias="byKey")
    maybe: Inner | None = Field(default=None, alias="maybeX")


class Populatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    item_name: str = Field(alias="itemName")


class OuterOfBoth(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    outer_name: str = Field(alias="outerName")
    both: Populatable
    only: Inner


class PopulatableSerializedByAlias(BaseModel):
    """Reads both forms, and its default dump is by alias, so the default and by-name differ."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)
    item_name: str = Field(alias="itemName")


class OuterOfBothByAlias(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    outer_name: str = Field(alias="outerName")
    both: PopulatableSerializedByAlias
    only: Inner


class Base(BaseModel):
    x: int = Field(alias="xx")


class SubByName(Base):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class OuterOverBase(BaseModel):
    """Declares `Base` for the field; the caller holds a subclass read by field name."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    outer_name: str = Field(alias="outerName")
    base: Base


class AppModel(BaseModel):
    """What a project puts at the top of its models: configuration and no fields."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class InnerOverAppModel(AppModel):
    item_name: str = Field(alias="itemName")


class OuterByAlias(BaseModel):
    outer_name: str = Field(alias="outerName")
    inner: InnerOverAppModel


class Plain(BaseModel):
    n: int


class BumpedOverAnnotatedSerializer(BaseModel):
    """No whole-value form reads back (the validator is not idempotent), and the serializer on the
    nested field reads a model's attribute, which the one-level-at-a-time copy does not hold."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    count: int = Field(alias="Count")
    inner: Annotated[Plain, PlainSerializer(lambda m: {"n": m.n})]

    @field_validator("count")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1


class StrictByNameInner(BaseModel):
    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)
    when: datetime = Field(alias="When")
    ident: UUID = Field(alias="Ident")


class AliasOuterOverStrictInner(BaseModel):
    """Read by alias; holds a strict model read by name, whose datetime and UUID are JSON text."""

    outer_name: str = Field(alias="outerName")
    inner: StrictByNameInner


class StrictAliasOuterOverStrictInner(BaseModel):
    model_config = ConfigDict(strict=True)
    outer_name: str = Field(alias="outerName")
    inner: StrictByNameInner


class StrictAliasOnlyInner(BaseModel):
    model_config = ConfigDict(strict=True)
    when: datetime = Field(alias="When")


class StrictByNameOuterOverStrictAliasInner(BaseModel):
    """The outer level is chosen by reading its by-name form as JSON; python mode refuses the inner
    level's datetime text."""

    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)
    outer_name: str = Field(alias="outerName")
    inner: StrictAliasOnlyInner


class StrictAliasBase(BaseModel):
    model_config = ConfigDict(strict=True)
    when: datetime = Field(alias="When")


class StrictSubByName(StrictAliasBase):
    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)


class ByNameOuterOverStrictBase(BaseModel):
    """Declares the strict alias-only base for its field and holds a subclass read by name: the
    base decides the inner level only if it reads the datetime text as JSON."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    outer_name: str = Field(alias="outerName")
    inner: StrictAliasBase


class StrictByNameBumped(BaseModel):
    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)
    when: datetime = Field(alias="When")
    n: int = Field(alias="N")

    @field_validator("n")
    @classmethod
    def _plus_one(cls, value: int) -> int:
        return value + 1


class AliasOuterOverStrictBumped(BaseModel):
    """The inner level reads back equal in no form; the form it accepts, as JSON, is the by-name one."""

    outer_name: str = Field(alias="outerName")
    inner: StrictByNameBumped


WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
IDENT = UUID("12345678-1234-5678-1234-567812345678")


class Leaf(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    leaf_name: str = Field(alias="leafName")


class Middle(BaseModel):
    middle: Leaf = Field(alias="middleLeaf")


class Top(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    top: Middle


class WithFieldSerializer(OuterByName):
    @field_serializer("outer_name")
    def _unchanged(self, value: str) -> str:
        return value


class WithModelSerializer(OuterByName):
    @model_serializer(mode="wrap")
    def _unchanged(self, handler):
        return handler(self)


class WithComputedField(OuterByName):
    @computed_field  # type: ignore[prop-decorator]
    @property
    def shout(self) -> str:
        return self.outer_name.upper()


class InnerWithFieldSerializer(Inner):
    @field_serializer("inner_name")
    def _unchanged(self, value: str) -> str:
        return value


class OuterOverSerializedInner(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    outer_name: str = Field(alias="outerName")
    inner: InnerWithFieldSerializer


class WithExtra(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True, extra="allow")
    outer_name: str = Field(alias="outerName")
    inner: Inner


class WithValidator(OuterByName):
    """A validator is not a serializer: it does not stop the form."""

    @field_validator("outer_name")
    @classmethod
    def _stripped(cls, value: str) -> str:
        return value.strip()


class ByNameDumpedByAlias(BaseModel):
    model_config = ConfigDict(
        serialize_by_alias=True, validate_by_alias=False, validate_by_name=True
    )
    level_name: str = Field(alias="levelName")


class AliasOuterOverByNameDumpedByAlias(BaseModel):
    """The inner level dumps by alias but reads only by name, so the path that sends it offers its
    by-name dump too."""

    outer_name: str = Field(alias="outerName")
    inner: ByNameDumpedByAlias


class FrozenInner(Inner):
    model_config = ConfigDict(frozen=True)


def _outer_of(items_type):
    return create_model(
        "OuterOf",
        __config__=ConfigDict(validate_by_alias=False, validate_by_name=True),
        outer_name=(str, Field(alias="outerName")),
        items=(items_type, ...),
    )


class PopulatableWithAToken(BaseModel):
    """Every read builds a new token, so no form reads back as the value that was sent."""

    model_config = ConfigDict(populate_by_name=True)
    item_name: str = Field(alias="itemName")
    token: str = Field(default_factory=lambda: uuid4().hex, exclude=True)


class OuterOverPopulatableWithAToken(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    outer_name: str = Field(alias="outerName")
    inner: PopulatableWithAToken


def _encode(value, annotation):
    return ServiceClient._encode(object.__new__(ServiceClient), value, annotation)


def _main_encoding(value, annotation):
    return TypeAdapter(annotation).dump_python(value, mode="json")


def _main_call_form(value):
    """What an rpc call path sent before: `to_jsonable_python`, by alias."""
    return TypeAdapter(type(value)).dump_python(value, mode="json", by_alias=True)


def _a_tree(cls):
    return cls(outer_name="o", inner=Inner(innerName="i"))


NESTED = [
    pytest.param(
        _a_tree(OuterByName),
        OuterByName,
        {"outer_name": "o", "inner": {"innerName": "i"}},
        id="inner-by-alias-in-outer-by-name",
    ),
    pytest.param(
        OuterOfMany(
            inners=[Inner(innerName="a")],
            by_key={"k": Inner(innerName="b")},
            maybe=Inner(innerName="c"),
        ),
        OuterOfMany,
        {
            "inners": [{"innerName": "a"}],
            "by_key": {"k": {"innerName": "b"}},
            "maybe": {"innerName": "c"},
        },
        id="in-a-list-a-dict-and-an-optional",
    ),
    pytest.param(
        Top(top=Middle(middleLeaf=Leaf(leaf_name="l"))),
        Top,
        {"top": {"middleLeaf": {"leaf_name": "l"}}},
        id="three-levels-each-different",
    ),
    pytest.param(
        _a_tree(WithValidator),
        WithValidator,
        {"outer_name": "o", "inner": {"innerName": "i"}},
        id="a-validator-does-not-stop-it",
    ),
    pytest.param(
        AliasOuterOverByNameDumpedByAlias(outerName="o", inner=ByNameDumpedByAlias(level_name="l")),
        AliasOuterOverByNameDumpedByAlias,
        {"outerName": "o", "inner": {"level_name": "l"}},
        id="inner-dumped-by-alias-but-read-by-name",
    ),
]


@pytest.mark.parametrize(("value", "annotation", "wire"), NESTED)
def test_each_level_of_a_tree_is_sent_in_the_form_its_class_reads_back(value, annotation, wire):
    sent = _encode(value, annotation)

    assert sent == wire
    assert TypeAdapter(annotation).validate_python(sent) == value


@pytest.mark.parametrize(("value", "annotation", "wire"), NESTED)
def test_an_rpc_call_argument_takes_the_same_form(value, annotation, wire):
    sent = wire_models({"item": value})

    assert sent == {"item": wire}
    assert annotation.model_validate(sent["item"]) == value


@pytest.mark.parametrize(
    ("items_type", "items"),
    [
        pytest.param(tuple[Inner, ...], (Inner(innerName="a"),), id="tuple"),
        pytest.param(set[FrozenInner], {FrozenInner(innerName="a")}, id="set"),
        pytest.param(
            frozenset[FrozenInner], frozenset({FrozenInner(innerName="a")}), id="frozenset"
        ),
    ],
)
def test_models_in_a_tuple_or_a_set_are_each_sent_in_the_form_their_class_reads(items_type, items):
    outer = _outer_of(items_type)
    value = outer(outer_name="o", items=items)
    wire = {"outer_name": "o", "items": [{"innerName": "a"}]}

    assert _encode(value, outer) == wire
    assert wire_models({"item": value}) == {"item": wire}


class KeepsANan(BaseModel):
    """Alias-only, and writes a NaN as itself where the level above it would write null."""

    model_config = ConfigDict(frozen=True, ser_json_inf_nan="constants")
    inner_name: str = Field(alias="innerName")
    x: float = 0.0


@pytest.mark.parametrize("container", [list, set, frozenset], ids=["list", "set", "frozenset"])
def test_a_model_in_a_set_keeps_the_form_it_wrote_when_the_level_above_writes_again(container):
    """The outer level's dump writes the items again under its own config; each model's own form
    is put back wherever it sits, in a set or a frozenset as in a list."""
    outer = _outer_of(container[KeepsANan])
    value = outer(outer_name="o", items=container([KeepsANan(innerName="a", x=float("nan"))]))

    for sent in (_encode(value, outer), wire_models({"item": value})["item"]):
        assert set(sent) == {"outer_name", "items"}, sent
        assert sent["outer_name"] == "o"
        [item] = sent["items"]
        assert set(item) == {"innerName", "x"}, item
        assert item["innerName"] == "a" and math.isnan(item["x"])


def test_a_tree_that_reads_back_in_no_form_goes_out_in_the_first_form_accepted():
    """No form reads back equal, so the first accepted one goes: the whole by-name dump, not the
    last form tried."""
    tree = OuterOverPopulatableWithAToken(
        outer_name="o", inner=PopulatableWithAToken(item_name="i")
    )

    assert _encode(tree, OuterOverPopulatableWithAToken) == {
        "outer_name": "o",
        "inner": {"item_name": "i"},
    }


def test_a_list_of_trees_is_sent_a_tree_at_a_time():
    value = [_a_tree(OuterByName), _a_tree(OuterByName)]

    sent = _encode(value, list[OuterByName])

    assert sent == [{"outer_name": "o", "inner": {"innerName": "i"}}] * 2


@pytest.mark.parametrize(
    "cls",
    [
        pytest.param(WithFieldSerializer, id="field-serializer"),
        pytest.param(WithModelSerializer, id="model-serializer"),
        pytest.param(WithComputedField, id="computed-field"),
    ],
)
def test_a_serializer_on_the_outer_model_sends_the_call_as_before(cls):
    """Each of these serializers returns what the model would have written, so the copy would
    read back equal: it is the guard, not the read-back, that sends the call as it was."""
    value = _a_tree(cls)

    assert _encode(value, cls) == _main_encoding(value, cls)
    assert wire_models({"item": value}) == {"item": _main_call_form(value)}
    with pytest.raises(ValueError):
        TypeAdapter(cls).validate_python(_encode(value, cls))


def test_a_serializer_on_an_inner_model_sends_the_call_as_before():
    value = OuterOverSerializedInner(outer_name="o", inner=InnerWithFieldSerializer(innerName="i"))

    assert _encode(value, OuterOverSerializedInner) == _main_encoding(
        value, OuterOverSerializedInner
    )
    assert wire_models({"item": value}) == {"item": _main_call_form(value)}


def test_a_model_with_extra_fields_sends_the_call_as_before():
    value = WithExtra(outer_name="o", inner=Inner(innerName="i"), unknown="u")

    assert _encode(value, WithExtra) == _main_encoding(value, WithExtra)
    assert wire_models({"item": value}) == {"item": _main_call_form(value)}


def test_the_callers_model_is_not_changed_by_writing_it():
    value = _a_tree(OuterByName)

    _encode(value, OuterByName)
    wire_models({"item": value})

    assert isinstance(value.inner, Inner)
    assert value == _a_tree(OuterByName)


def test_writing_a_tree_raises_no_serializer_warning():
    """The copy holds dicts where the fields are typed as models; pydantic's warning about that
    is not the caller's to see."""
    value = _a_tree(OuterByName)
    sent = {"outer_name": "o", "inner": {"innerName": "i"}}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert _encode(value, OuterByName) == sent
        assert wire_models({"item": value}) == {"item": sent}

    assert [str(w.message) for w in caught] == []


def test_a_level_that_reads_either_form_follows_the_order_of_the_path_that_sends_it():
    """`Populatable` reads back in both forms. `_encode` tries the model's default first, so it is
    written by field name; the rpc call paths try the alias first, as `to_jsonable_python` did."""
    value = OuterOfBoth(outer_name="o", both=Populatable(itemName="b"), only=Inner(innerName="i"))

    assert _encode(value, OuterOfBoth) == {
        "outer_name": "o",
        "both": {"item_name": "b"},
        "only": {"innerName": "i"},
    }
    assert wire_models({"item": value})["item"] == {
        "outer_name": "o",
        "both": {"itemName": "b"},
        "only": {"innerName": "i"},
    }


def test_a_level_that_reads_either_form_is_written_by_encode_in_its_default_dump():
    """The model's default comes first on the `_encode` path, so a level whose default dump is by
    alias is written by alias there, not by field name."""
    value = OuterOfBothByAlias(
        outer_name="o", both=PopulatableSerializedByAlias(itemName="b"), only=Inner(innerName="i")
    )

    assert _encode(value, OuterOfBothByAlias) == {
        "outer_name": "o",
        "both": {"itemName": "b"},
        "only": {"innerName": "i"},
    }


def test_a_nested_subclass_keeps_the_alias_form_its_declared_base_reads():
    """The field is declared as `Base`, so the service reads the nested value as `Base`: the alias
    form that `Base` reads back is kept even though the subclass held there refuses it."""
    value = OuterOverBase.model_construct(outer_name="o", base=SubByName(x=1))

    sent = wire_models({"item": value})["item"]

    assert sent == {"outer_name": "o", "base": {"xx": 1}}
    assert OuterOverBase.model_validate(sent).base == Base(xx=1)


def test_a_level_over_a_base_that_holds_only_configuration_is_written_by_the_form_it_reads():
    """The inner model reads by field name and its base holds only `model_config`, which accepts
    any form: the base says nothing, so the inner level takes the field-name form."""
    value = OuterByAlias(outerName="o", inner=InnerOverAppModel(item_name="i"))

    sent = wire_models({"item": value})["item"]

    assert sent == {"outerName": "o", "inner": {"item_name": "i"}}
    assert TypeAdapter(OuterByAlias).validate_python(sent) == value
    assert _encode(value, OuterByAlias) == sent


def test_a_serializer_in_annotated_that_cannot_write_the_copy_leaves_the_call_as_it_was():
    """Building the one-level form raises inside the serializer. That form is unavailable, and the
    call goes out in the first whole-value form accepted, the field-name one; nothing raises before
    sending."""
    value = BumpedOverAnnotatedSerializer(count=1, inner=Plain(n=2))
    accepted = {"count": 2, "inner": {"n": 2}}

    assert _encode(value, BumpedOverAnnotatedSerializer) == accepted
    assert wire_models({"item": value}) == {"item": accepted}
    BumpedOverAnnotatedSerializer.model_validate(accepted)


@pytest.mark.parametrize(
    ("value", "annotation", "wire"),
    [
        pytest.param(
            AliasOuterOverStrictInner(
                outerName="o", inner=StrictByNameInner(when=WHEN, ident=IDENT)
            ),
            AliasOuterOverStrictInner,
            {
                "outerName": "o",
                "inner": {"when": "2026-01-02T03:04:05Z", "ident": str(IDENT)},
            },
            id="strict-by-name-inner-in-an-alias-outer",
        ),
        pytest.param(
            StrictAliasOuterOverStrictInner(
                outerName="o", inner=StrictByNameInner(when=WHEN, ident=IDENT)
            ),
            StrictAliasOuterOverStrictInner,
            {
                "outerName": "o",
                "inner": {"when": "2026-01-02T03:04:05Z", "ident": str(IDENT)},
            },
            id="strict-alias-outer-over-a-strict-by-name-inner",
        ),
        pytest.param(
            StrictByNameOuterOverStrictAliasInner(
                outer_name="o", inner=StrictAliasOnlyInner(When=WHEN)
            ),
            StrictByNameOuterOverStrictAliasInner,
            {"outer_name": "o", "inner": {"When": "2026-01-02T03:04:05Z"}},
            id="strict-by-name-outer-over-a-strict-alias-inner",
        ),
    ],
)
def test_each_level_of_a_strict_tree_is_read_as_the_service_reads_it(value, annotation, wire):
    """A strict level refuses the JSON text of its own dump in python mode; each level's forms are
    read python mode then JSON mode, so the level takes the form the service reads."""
    assert _encode(value, annotation) == wire
    assert wire_models({"item": value})["item"] == wire


def test_a_strict_base_decides_a_nested_level_only_as_the_service_reads_it():
    """On the rpc call path the inner level's first form is the alias one, which the subclass
    refuses, so its bases are asked, and the strict base reads that form only as JSON. (On
    `_encode`'s path the first form is the subclass's own field-name form, which it reads, so it is
    sent: the mirror of the subclass limit, stated in the API reference.)"""
    value = ByNameOuterOverStrictBase.model_construct(
        outer_name="o", inner=StrictSubByName(when=WHEN)
    )

    assert wire_models({"item": value})["item"] == {
        "outer_name": "o",
        "inner": {"When": "2026-01-02T03:04:05Z"},
    }


def test_a_nested_level_that_reads_back_in_no_form_takes_the_form_the_service_accepts():
    value = AliasOuterOverStrictBumped(outerName="o", inner=StrictByNameBumped(when=WHEN, n=1))

    for sent in (wire_models({"item": value})["item"], _encode(value, AliasOuterOverStrictBumped)):
        assert sent["outerName"] == "o"
        assert list(sent["inner"]) == ["when", "n"], sent


class DInner(BaseModel):
    """Reads by field name only, so its alias form `{"xx": 5}` is accepted and read as the defaults."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(0, alias="xx")


class AliasOuterOverDInner(BaseModel):
    outer_name: str = Field(alias="outerName")
    inner: DInner


D_TREE = AliasOuterOverDInner(outerName="o", inner=DInner(x=5))
D_WIRE = {"outerName": "o", "inner": {"x": 5}}


@pytest.mark.parametrize(
    ("annotation", "value", "wire"),
    [
        pytest.param(AliasOuterOverDInner, D_TREE, D_WIRE, id="direct"),
        pytest.param(list[AliasOuterOverDInner], [D_TREE], [D_WIRE], id="list"),
    ],
)
def test_a_level_whose_alias_form_is_read_as_its_defaults_is_sent_by_name(annotation, value, wire):
    """The alias form of the inner level is accepted by its class and read as `x=0`; the first form
    that reads back EQUAL is sent, ahead of any form that is merely accepted."""
    assert _encode(value, annotation) == wire
    if annotation is AliasOuterOverDInner:
        assert wire_models({"item": value}) == {"item": wire}

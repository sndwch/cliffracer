"""Settings the model reads and writes back are accepted, and a serializer that loses items is refused.

`normalize` checks that every stored field is written under a key its model reads. A stdlib
dataclass has no validation config of its own: the model or pydantic dataclass that holds it decides
which aliases apply to its fields. The check read the dataclass's aliases from the first matching
node anywhere in the root model's schema, and that schema embeds nested models with their own
configs, so a dataclass used under two models that configure aliases differently was refused under
whichever was not found first, although pydantic reads and writes it under each model's own config.
The aliases are now read from the schema of the nearest holder, without entering another model or
pydantic dataclass, and the cases below put the models in both orders and two levels deep. A
serializer that returns fewer items than a list holds failed the check with a bare `ValueError` from
`zip`, where every other lossy serializer is a `TemplateError`.
"""

import dataclasses

import pytest
from pydantic import BaseModel, ConfigDict, Field, Json, RootModel, create_model, field_serializer
from pydantic.dataclasses import dataclass as pydantic_dataclass

from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.shipment_templates import shipment_template

pytestmark = pytest.mark.unit


def to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


@dataclasses.dataclass
class Dimensions:
    box_width: int = 1


def registered(model):
    return TemplateCatalog().register(shipment_template(settings_model=model))


class Plain(BaseModel):
    dimensions: Dimensions = Dimensions()


class Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    top: Dimensions = Dimensions()
    plain: Plain = Plain()


@pydantic_dataclass
class Holder:
    dimensions: Dimensions = dataclasses.field(default_factory=Dimensions)


class CamelHolding(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    top: Dimensions = Dimensions()
    holder: Holder = Holder()


DOCUMENT = {"top": {"boxWidth": 2}, "plain": {"dimensions": {"box_width": 3}}}


def test_a_dataclass_under_two_models_with_different_aliases_is_read_under_each_models_own():
    accepted = registered(Camel).normalize(DOCUMENT)

    assert accepted.materialize() == Camel.model_validate(DOCUMENT)
    assert accepted == registered(Camel).normalize(accepted.materialize())


def test_a_dataclass_inside_a_pydantic_dataclass_is_read_under_the_pydantic_dataclasss_own_schema():
    document = {"top": {"boxWidth": 2}, "holder": {"dimensions": {"box_width": 3}}}

    accepted = registered(CamelHolding).normalize(document)

    assert accepted.materialize() == CamelHolding.model_validate(document)


@dataclasses.dataclass
class Crate:
    box_dimensions: Dimensions = dataclasses.field(default_factory=Dimensions)


class PlainCrate(BaseModel):
    crate: Crate = Crate()


@pydantic_dataclass
class HolderOfCrate:
    crate: Crate = dataclasses.field(default_factory=Crate)


@pydantic_dataclass(config=ConfigDict(alias_generator=to_camel, populate_by_name=True))
class CamelHolderOfDimensions:
    dimensions: Dimensions = dataclasses.field(default_factory=Dimensions)


def camel_model(**fields):
    """A camel-case model whose fields are declared in the order given."""
    return create_model(
        "CamelModel",
        __config__=ConfigDict(alias_generator=to_camel, populate_by_name=True),
        **{name: (kind, kind()) for name, kind in fields.items()},
    )


def plain_model(**fields):
    return create_model("PlainModel", **{name: (kind, kind()) for name, kind in fields.items()})


CASES = {
    "plain-model-before": (
        camel_model(plain=Plain, top=Dimensions),
        {"top": {"boxWidth": 2}, "plain": {"dimensions": {"box_width": 3}}},
    ),
    "plain-model-after": (
        camel_model(top=Dimensions, plain=Plain),
        {"top": {"boxWidth": 2}, "plain": {"dimensions": {"box_width": 3}}},
    ),
    "two-levels-plain-model-before": (
        camel_model(plain=PlainCrate, top=Crate),
        {
            "top": {"boxDimensions": {"boxWidth": 2}},
            "plain": {"crate": {"box_dimensions": {"box_width": 3}}},
        },
    ),
    "two-levels-plain-model-after": (
        camel_model(top=Crate, plain=PlainCrate),
        {
            "top": {"boxDimensions": {"boxWidth": 2}},
            "plain": {"crate": {"box_dimensions": {"box_width": 3}}},
        },
    ),
    "two-levels-pydantic-dataclass-before": (
        camel_model(holder=HolderOfCrate, top=Crate),
        {
            "top": {"boxDimensions": {"boxWidth": 2}},
            "holder": {"crate": {"box_dimensions": {"box_width": 3}}},
        },
    ),
    "two-levels-pydantic-dataclass-after": (
        camel_model(top=Crate, holder=HolderOfCrate),
        {
            "top": {"boxDimensions": {"boxWidth": 2}},
            "holder": {"crate": {"box_dimensions": {"box_width": 3}}},
        },
    ),
    "camel-pydantic-dataclass-before-plain": (
        plain_model(camel_holder=CamelHolderOfDimensions, plain=Plain),
        {
            "camel_holder": {"dimensions": {"boxWidth": 2}},
            "plain": {"dimensions": {"box_width": 3}},
        },
    ),
    "camel-pydantic-dataclass-after-plain": (
        plain_model(plain=Plain, camel_holder=CamelHolderOfDimensions),
        {
            "camel_holder": {"dimensions": {"boxWidth": 2}},
            "plain": {"dimensions": {"box_width": 3}},
        },
    ),
}


def camel_model_of(annotation, default):
    return create_model(
        "CamelModel",
        __config__=ConfigDict(alias_generator=to_camel, populate_by_name=True),
        many=(annotation, default),
        plain=(Plain, Plain()),
    )


CASES["list-under-a-camel-model"] = (
    camel_model_of(list[Dimensions], [Dimensions()]),
    {"many": [{"boxWidth": 2}], "plain": {"dimensions": {"box_width": 3}}},
)
CASES["dict-under-a-camel-model"] = (
    camel_model_of(dict[str, Dimensions], {"a": Dimensions()}),
    {"many": {"a": {"boxWidth": 2}}, "plain": {"dimensions": {"box_width": 3}}},
)
CASES["tuple-under-a-camel-model"] = (
    camel_model_of(tuple[Dimensions, ...], (Dimensions(),)),
    {"many": [{"boxWidth": 2}], "plain": {"dimensions": {"box_width": 3}}},
)


class CamelRoot(RootModel[dict[str, Dimensions]]):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


CASES["root-model-of-a-dict"] = (CamelRoot, {"a": {"boxWidth": 2}})


class Node(BaseModel):
    dimensions: Dimensions = Dimensions()
    child: "Node | None" = None


Node.model_rebuild()


# A nested model used twice, or one that refers to itself, is held in the schema's `definitions`,
# so the root of the search is a `definitions` node and not the holder's own.
CASES["a-nested-model-used-twice"] = (
    create_model(
        "CamelTwice",
        __config__=ConfigDict(alias_generator=to_camel, populate_by_name=True),
        first=(Plain, Plain()),
        second=(Plain, Plain()),
        top=(Dimensions, Dimensions()),
    ),
    {
        "first": {"dimensions": {"box_width": 2}},
        "second": {"dimensions": {"box_width": 3}},
        "top": {"boxWidth": 4},
    },
)
CASES["a-nested-model-that-refers-to-itself"] = (
    create_model(
        "CamelNodes",
        __config__=ConfigDict(alias_generator=to_camel, populate_by_name=True),
        node=(Node, Node(child=Node())),
        top=(Dimensions, Dimensions()),
    ),
    {
        "node": {
            "dimensions": {"box_width": 2},
            "child": {"dimensions": {"box_width": 3}, "child": None},
        },
        "top": {"boxWidth": 4},
    },
)


@dataclasses.dataclass(frozen=True)
class FrozenDimensions:
    box_width: int = 1


@dataclasses.dataclass(frozen=True)
class FrozenCrate:
    box_dimensions: FrozenDimensions = FrozenDimensions()


class PlainSet(BaseModel):
    many: frozenset[FrozenDimensions] = frozenset({FrozenDimensions()})


class PlainCrateSet(BaseModel):
    many: frozenset[FrozenCrate] = frozenset({FrozenCrate()})


@pydantic_dataclass
class HolderOfSet:
    many: frozenset[FrozenDimensions] = dataclasses.field(
        default_factory=lambda: frozenset({FrozenDimensions()})
    )


def camel_model_with(**fields):
    """A camel-case model of `name=(annotation, default)` fields, in the order given."""
    return create_model(
        "CamelModel",
        __config__=ConfigDict(alias_generator=to_camel, populate_by_name=True),
        **fields,
    )


# A set's items are searched for in the schema before the set's holder is known, so a model or
# pydantic dataclass that holds the same dataclass in a set of its own must stop the search there.
CASES["set-plain-model-before"] = (
    camel_model_with(
        plain=(PlainSet, PlainSet()),
        top=(frozenset[FrozenDimensions], frozenset({FrozenDimensions()})),
    ),
    {"plain": {"many": [{"box_width": 3}]}, "top": [{"boxWidth": 2}]},
)
CASES["set-pydantic-dataclass-before"] = (
    camel_model_with(
        holder=(HolderOfSet, HolderOfSet()),
        top=(frozenset[FrozenDimensions], frozenset({FrozenDimensions()})),
    ),
    {"holder": {"many": [{"box_width": 3}]}, "top": [{"boxWidth": 2}]},
)
CASES["set-two-levels-plain-model-before"] = (
    camel_model_with(
        plain=(PlainCrateSet, PlainCrateSet()),
        top=(frozenset[FrozenCrate], frozenset({FrozenCrate()})),
    ),
    {
        "plain": {"many": [{"box_dimensions": {"box_width": 3}}]},
        "top": [{"boxDimensions": {"boxWidth": 2}}],
    },
)


@pytest.mark.parametrize(("model", "document"), CASES.values(), ids=list(CASES))
def test_a_dataclass_is_read_under_the_model_that_holds_it_wherever_the_models_sit(model, document):
    accepted = registered(model).normalize(document)

    assert accepted.materialize() == model.model_validate(document)
    assert registered(model).normalize(accepted.materialize()) == accepted


def test_the_accepted_document_is_what_pydantic_writes():
    accepted = registered(Camel).normalize(DOCUMENT)

    assert accepted._json == ('{"plain":{"dimensions":{"box_width":3}},"top":{"boxWidth":2}}')


class WithJson(BaseModel):
    payload: Json[dict[str, int]] = '{"a": 1}'


def test_the_accepted_document_is_pydantics_round_trip_dump():
    """A `Json` field is written back as the JSON text it was read from, not as the parsed value."""
    accepted = registered(WithJson).normalize({"payload": '{"a": 2}'})

    assert accepted.materialize().payload == {"a": 2}
    assert accepted._json == '{"payload":"{\\"a\\":2}"}'


def test_CONTROL_a_nested_model_that_cannot_read_its_serialized_key_is_still_refused():
    class Name(BaseModel):
        model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
        width: int = Field(alias="w", default=1)

    class Outer(BaseModel):
        model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
        top: Dimensions = Dimensions()
        inner: Name = Name()

    with pytest.raises(TemplateError, match="width.*round-trip"):
        registered(Outer).normalize({"top": {"boxWidth": 2}, "inner": {"width": 4}})


class FrozenName(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True, frozen=True)
    width: int = Field(alias="w", default=1)


@pytest.mark.parametrize(
    ("annotation", "default"),
    [
        (list[FrozenName], [FrozenName()]),
        (tuple[FrozenName, ...], (FrozenName(),)),
        (set[FrozenName], {FrozenName()}),
        (frozenset[FrozenName], frozenset({FrozenName()})),
    ],
    ids=["list", "tuple", "set", "frozenset"],
)
@pytest.mark.parametrize("width", [1, 4])
def test_CONTROL_a_nested_model_in_a_container_that_cannot_read_its_serialized_key_is_still_refused(
    annotation, default, width
):
    outer = create_model(
        "Outer",
        __config__=ConfigDict(alias_generator=to_camel, populate_by_name=True),
        top=(Dimensions, Dimensions()),
        inner=(annotation, default),
    )

    with pytest.raises(TemplateError, match="width.*round-trip"):
        registered(outer).normalize({"top": {"boxWidth": 2}, "inner": [{"width": width}]})


def test_CONTROL_a_dataclass_whose_own_model_cannot_read_its_alias_is_still_refused():
    class Unreadable(BaseModel):
        model_config = ConfigDict(alias_generator=to_camel, validate_by_alias=False)
        top: Dimensions = Dimensions()

    with pytest.raises(TemplateError, match="box_width.*round-trip"):
        registered(Unreadable).normalize({"top": {"box_width": 2}})


class Keeps(BaseModel):
    values: list[int] = []
    pair: tuple[int, ...] = ()


class DropsListItems(Keeps):
    @field_serializer("values")
    def first_only(self, values):
        return values[:1]


class DropsTupleItems(Keeps):
    @field_serializer("pair")
    def first_only(self, pair):
        return pair[:1]


class AddsListItems(Keeps):
    @field_serializer("values")
    def twice(self, values):
        return [*values, *values]


@pytest.mark.parametrize(
    "model",
    [DropsListItems, DropsTupleItems, AddsListItems],
    ids=["drops-list", "drops-tuple", "adds"],
)
def test_a_serializer_that_changes_the_item_count_is_a_template_error(model):
    with pytest.raises(TemplateError, match="round-trip"):
        registered(model).normalize({"values": [1, 2, 3], "pair": [4, 5]})


def test_CONTROL_a_serializer_that_keeps_every_item_is_accepted():
    accepted = registered(Keeps).normalize({"values": [1, 2, 3], "pair": [4, 5]})

    assert accepted.materialize().values == [1, 2, 3]
    assert accepted.materialize().pair == (4, 5)

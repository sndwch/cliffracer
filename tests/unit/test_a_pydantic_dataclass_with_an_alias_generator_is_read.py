"""A pydantic dataclass whose config generates aliases is read back under them.

`normalize` checks that every stored field is written under a key its model reads. A model's
`alias_generator` writes the alias it generates into the field's info, and the check read the
serialized key from there. A pydantic dataclass applies its `alias_generator` in its validation
schema and leaves the field info without an alias, so a field such as `box_width`, written as
`boxWidth`, was refused as one that cannot round-trip, although pydantic reads and writes it back.
The check reads the aliases of a pydantic dataclass from its schema, as it does for the stdlib
dataclasses a model holds.
"""

import dataclasses
import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from pydantic.dataclasses import dataclass as pydantic_dataclass

from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.shipment_templates import shipment_template

pytestmark = pytest.mark.unit


def to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True)
ALIAS_ONLY = ConfigDict(alias_generator=to_camel)


def registered(model):
    return TemplateCatalog().register(shipment_template(settings_model=model))


@pydantic_dataclass(config=CAMEL)
class Box:
    box_width: int = 1
    box_height: int = 1


@pydantic_dataclass(config=ALIAS_ONLY)
class AliasOnlyBox:
    box_width: int = 1


@dataclasses.dataclass
class Crate:
    box_depth: int = 1


@pydantic_dataclass(config=CAMEL)
class CrateHolder:
    crate_inside: Crate = dataclasses.field(default_factory=Crate)


@pydantic_dataclass(config=CAMEL)
class BoxHolder:
    inner_box: Box = dataclasses.field(default_factory=Box)


class Plain(BaseModel):
    box: Box = Box()


def holding(**fields):
    return create_model("Holding", **{name: (kind, kind()) for name, kind in fields.items()})


def camel_holding(**fields):
    return create_model(
        "CamelHolding",
        __config__=CAMEL,
        **{name: (kind, kind()) for name, kind in fields.items()},
    )


CASES = {
    "under-a-plain-model": (
        holding(box=Box),
        {"box": {"boxWidth": 2, "boxHeight": 3}},
    ),
    "under-a-camel-model": (
        camel_holding(main_box=Box),
        {"mainBox": {"boxWidth": 2, "boxHeight": 3}},
    ),
    "alias-only-config": (
        holding(box=AliasOnlyBox),
        {"box": {"boxWidth": 2}},
    ),
    "inside-another-pydantic-dataclass": (
        holding(holder=BoxHolder),
        {"holder": {"innerBox": {"boxWidth": 2, "boxHeight": 3}}},
    ),
    "holding-a-stdlib-dataclass": (
        holding(holder=CrateHolder),
        {"holder": {"crateInside": {"boxDepth": 2}}},
    ),
    "next-to-a-model-that-writes-the-same-dataclass-by-name": (
        holding(box=Box, plain=Plain),
        {"box": {"boxWidth": 2}, "plain": {"box": {"boxWidth": 3}}},
    ),
}


@pydantic_dataclass(config=CAMEL)
class BoxReadBeforeItsFields:
    """A before-validator wraps the dataclass's schema, so its field aliases sit one node deeper."""

    box_width: int = 1

    @model_validator(mode="before")
    @classmethod
    def _unchanged(cls, data):
        return data


@pydantic_dataclass(config=CAMEL)
class BoxWithANullLabel:
    box_label: str | None = None


@pydantic_dataclass(config=CAMEL)
class BoxReadThroughAChain:
    """Two before-validators and a wrap validator: the field aliases sit two wrappers deep."""

    box_width: int = 1

    @model_validator(mode="before")
    @classmethod
    def _first(cls, data):
        return data

    @model_validator(mode="before")
    @classmethod
    def _second(cls, data):
        return data

    @model_validator(mode="wrap")
    @classmethod
    def _around(cls, data, handler):
        return handler(data)


CASES["with-a-before-model-validator"] = (
    holding(box=BoxReadBeforeItsFields),
    {"box": {"boxWidth": 2}},
)
CASES["with-a-chain-of-model-validators"] = (
    holding(box=BoxReadThroughAChain),
    {"box": {"boxWidth": 2}},
)
CASES["a-null-under-a-generated-alias"] = (
    holding(box=BoxWithANullLabel),
    {"box": {"boxLabel": None}},
)
CASES["a-stdlib-dataclass-held-by-a-camel-model"] = (
    camel_holding(crate=Crate),
    {"crate": {"boxDepth": 2}},
)


@pytest.mark.parametrize(("model", "document"), CASES.values(), ids=list(CASES))
def test_a_pydantic_dataclass_with_generated_aliases_is_accepted_and_round_trips(model, document):
    accepted = registered(model).normalize(document)

    assert accepted.materialize() == model.model_validate(document)
    assert registered(model).normalize(accepted.materialize()) == accepted


def test_the_accepted_document_is_written_under_the_generated_aliases():
    accepted = registered(holding(box=Box)).normalize({"box": {"boxWidth": 2}})

    assert json.loads(accepted._json) == {"box": {"boxWidth": 2, "boxHeight": 1}}


def test_CONTROL_a_pydantic_dataclass_that_cannot_read_its_generated_aliases_is_still_refused():
    @pydantic_dataclass(
        config=ConfigDict(alias_generator=to_camel, validate_by_alias=False, validate_by_name=True)
    )
    class Unreadable:
        box_width: int = 1

    with pytest.raises(TemplateError, match="box_width.*round-trip"):
        registered(holding(box=Unreadable)).normalize({"box": {"box_width": 2}})


def test_CONTROL_an_explicit_alias_on_a_pydantic_dataclass_field_is_still_read():
    @pydantic_dataclass(config=ConfigDict(populate_by_name=True))
    class Labelled:
        box_width: int = Field(default=1, alias="width")

    model = holding(label=Labelled)
    accepted = registered(model).normalize({"label": {"width": 2}})

    assert accepted.materialize() == model.model_validate({"label": {"width": 2}})


def test_CONTROL_a_one_word_field_is_unchanged():
    @pydantic_dataclass(config=CAMEL)
    class OneWord:
        width: int = 1

    model = holding(one=OneWord)

    assert registered(model).normalize({"one": {"width": 2}}).materialize().one.width == 2


@dataclasses.dataclass
class Plainly:
    width: int = 1


@pytest.mark.parametrize("instance", [Plainly(), Box()], ids=["stdlib", "pydantic"])
def test_CONTROL_a_dataclass_held_in_an_untyped_field_is_a_template_error(
    instance,
):
    class Untyped(BaseModel):
        anything: Any = None

    with pytest.raises(TemplateError):
        registered(Untyped).normalize({"anything": instance})
